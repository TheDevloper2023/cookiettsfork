import sys
import os
import numpy as np
import random
import time
import argparse
import torch
import matplotlib.pyplot as plt
from scipy.io.wavfile import write
import json
import re
import difflib
from glob import glob
from unidecode import unidecode
import nltk # sentence spliting
from nltk import sent_tokenize
from CookieTTS._2_ttm.tacotron2_tm.model import load_model
from CookieTTS.utils.text import text_to_sequence
from CookieTTS.utils.dataset.utils import load_filepaths_and_text
from CookieTTS.utils.model.utils import alignment_metric
############/Denoiser/######################################################
from CookieTTS._4_mtw.hifi.denoiser import Denoiser
from CookieTTS._4_mtw.hifi.env import AttrDict 
from CookieTTS._4_mtw.hifi.models import Generator 
from CookieTTS._4_mtw.hifi.meldataset import MAX_WAV_VALUE
import scipy.signal
############################################################################

def get_mask_from_lengths(lengths, max_len=None):
    if not max_len:
        max_len = torch.max(lengths).long()
    ids = torch.arange(0, max_len, device=lengths.device, dtype=torch.int64)
    mask = (ids < lengths.unsqueeze(1))
    return mask


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    # list(chunks([0,1,2,3,4,5,6,7,8,9],2)) -> [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]]
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# generator for text splitting.
def parse_text_into_segments(texts, split_at_quotes=True, target_segment_length=200, split_at_newline=True):
    """Swap speaker at every quote mark. Each split segment will have quotes around it (for information later on rather than accuracy to the original text)."""
    
    # split text by quotes
    quo ='"' # nested quotes in list comprehension are hard to work with
    wsp =' '
    texts = [f'"{text.replace(quo,"").strip(wsp)}"' if i%2 else text.replace(quo,"").strip(wsp) for i, text in enumerate(unidecode(texts).split('"'))]
    
    # clean up and remove empty texts
    def clean_text(text):
        text = (text.strip(" ")
                   #.replace("\n"," ")
                    .replace("  "," ")
                    .replace("> --------------------------------------------------------------------------","")
                    .replace("------------------------------------",""))
        return text
    texts = [clean_text(text) for text in texts if len(text.replace('"','').strip(' ')) or len(clean_text(text))]
    assert len(texts)
    
    # split text by sentences and add commas in where needed.
    def quotify(seg, text):
        if '"' in text:
            if seg[0] != '"': seg='"'+seg
            if seg[-1] != '"': seg+='"'
        return seg
    texts_tmp = []
    for text in texts:
        if len(text.strip()):
            for x in sent_tokenize(text):
                if len(x.replace('"','').strip(' ')):
                    texts_tmp.extend([quotify(x.strip(" "), text)])
        else:
            if len(text.replace('"','').strip(' ')):
                texts_tmp.extend([quotify(text.strip(" "), text)])
    #texts = [texts_tmp.extend([quotify(x.strip(" "), text) for x in sent_tokenize(text) if len(x.replace('"','').strip(' '))]) for text in texts]
    texts = texts_tmp
    del texts_tmp
    assert len(texts)
    
    # merge neighbouring sentences
    quote_mode = False
    texts_output = []
    texts_segmented = ''
    texts_len = len(texts)
    for i, text in enumerate(texts):
        
        # split segment if quote swap
        if split_at_quotes and ('"' in text and quote_mode == False) or (not '"' in text and quote_mode == True):
            texts_output.append(texts_segmented.replace('"','').replace("\n","").strip())
            texts_segmented=text
            quote_mode = not quote_mode
        # if the prev text is already longer than the target length
        elif len(texts_segmented) > target_segment_length:
            text = text.replace('"','')
            while len(texts_segmented):
                texts_segmented_parts = texts_segmented.split(',')
                texts_segmented = ''
                texts_segmented_overflow = ''
                for i, part in enumerate(texts_segmented_parts):
                    
                    if split_at_newline and part.strip(' ').endswith('\n'):
                        part = part.replace('\n','')
                        texts_segmented += part if i == 0 else f',{part}'
                        texts_segmented_overflow = ','.join(texts_segmented_parts[i+1:])
                        break
                    elif split_at_newline and part.strip(' ').startswith('\n'):
                        part = part.replace('\n','')
                        texts_segmented = ''
                        texts_segmented_overflow = ','.join(texts_segmented_parts[i+1:])
                        break
                    elif i > 0 and len(texts_segmented)+len(part.replace('\n','')) > target_segment_length:
                        texts_segmented_overflow = ','.join(texts_segmented_parts[i:])
                        break
                    else:
                        part = part.replace('\n','')
                        texts_segmented += part if i == 0 else f',{part}'
                
                if len(texts_segmented.replace('\n','').strip()):
                    texts_output.append(texts_segmented.replace('\n','').strip())
                    texts_segmented = ''
                if len(texts_segmented_overflow):
                    texts_segmented = texts_segmented_overflow
                del texts_segmented_overflow, texts_segmented_parts
            texts_segmented=text
        
        else: # continue adding to segment
            text = text.replace('"','')
            texts_segmented+= f' {text}'
    
    # add any remaining stuff.
    while len(texts_segmented):
        texts_segmented_parts = texts_segmented.split(',')
        texts_segmented = ''
        texts_segmented_overflow = ''
        for i, part in enumerate(texts_segmented_parts):
            if i > 0 and len(texts_segmented)+len(part) > target_segment_length:
                texts_segmented_overflow = ','.join(texts_segmented_parts[i:])
                break
            else:
                texts_segmented += part if i == 0 else f',{part}'
        if len(texts_segmented.strip()):
            texts_output.append(texts_segmented.strip())
        texts_segmented = ''
        if len(texts_segmented_overflow):
            texts_segmented = texts_segmented_overflow
        del texts_segmented_overflow, texts_segmented_parts
    
    assert len(texts_output)
    
    return texts_output


def get_first_over_thresh(x, threshold):
    """Takes [B, T] and outputs first T over threshold for each B (output.shape = [B])."""
    device = x.device
    x = x.clone().cpu().float() # using CPU because GPU implementation of argmax() splits tensor into 32 elem chunks, each chunk is parsed forward then the outputs are collected together... backwards
    x[:,-1] = threshold # set last to threshold just incase the output didn't finish generating.
    x[x>threshold] = threshold
    if int(''.join(torch.__version__.split('+')[0].split('.'))) < 170:
        return ( (x.size(1)-1)-(x.flip(dims=(1,)).argmax(dim=1)) ).to(device).int()
    else:
        return x.argmax(dim=1).to(device).int()


class T2S:
    def __init__(self, conf):
        self.conf = conf
        self.hconf = None
        torch.set_grad_enabled(False)
        # load Tacotron2
        self.ttm_current = self.conf['TTM']['default_model']
        assert self.ttm_current in self.conf['TTM']['models'].keys(), "Tacotron default model not found in config models"
        tacotron_path = self.conf['TTM']['models'][self.ttm_current]['modelpath'] # get first available Tacotron
        self.tacotron, self.ttm_hparams, self.ttm_sp_name_lookup, self.ttm_sp_id_lookup = self.load_tacotron2(tacotron_path)
        
        # load HiFi-GAN
        self.MTW_current = self.conf['MTW']['default_model']
        assert self.MTW_current in self.conf['MTW']['models'].keys(), "HiFi-GAN default model not found in config models"
        vocoder_path = self.conf['MTW']['models'][self.MTW_current]['modelpath']
        self.vocoder, self.MTW_conf = self.load_hifigan(vocoder_path)
        
        # load torchMoji
        self.tm_sentence_tokenizer, self.tm_torchmoji = self.load_torchmoji()
        
        # override since my checkpoints are still missing speaker names
        if self.conf['TTM']['use_speaker_ids_file_override']:
            speaker_ids_fpath = self.conf['TTM']['speaker_ids_file']
            self.ttm_sp_name_lookup = {name: self.ttm_sp_id_lookup[int(ext_id)] for _, name, ext_id in load_filepaths_and_text(speaker_ids_fpath)}
        
        # load arpabet/pronounciation dictionary
        dict_path = self.conf['dict_path']
        self.load_arpabet_dict(dict_path)
        
        # download nltk package for splitting text into sentences
        nltk.download('punkt')
        
        print("T2S Initialized and Ready!")
    
    
    def load_arpabet_dict(self, dict_path):
        print("Loading ARPAbet Dictionary... ", end="")
        self.arpadict = {}
        for line in reversed((open(dict_path, "r").read()).splitlines()):
            self.arpadict[(line.split(" ", 1))[0]] = (line.split(" ", 1))[1].strip()
        print("Done!")
    
    
    def ARPA(self, text, punc=r"!?,.;:␤#-_'\"()[]"):
        text = text.replace("\n"," ")
        out = ''
        for word in text.split(" "):
            end_chars = ''; start_chars = ''
            while any(elem in word for elem in punc) and len(word) > 1:
                if word[-1] in punc: end_chars = word[-1] + end_chars; word = word[:-1]
                elif word[0] in punc: start_chars = start_chars + word[0]; word = word[1:]
                else: break
            if word.upper() in self.arpadict.keys():
                word = "{" + str(self.arpadict[word.upper()]) + "}"
            out = (out + " " + start_chars + word + end_chars).strip()
        return out
    
    
    def load_torchmoji(self):
        """ Use torchMoji to score texts for emoji distribution.
        
        The resulting emoji ids (0-63) correspond to the mapping
        in emoji_overview.png file at the root of the torchMoji repo.
        
        Writes the result to a csv file.
        """
        import json
        import numpy as np
        import os
        from CookieTTS.utils.torchmoji.sentence_tokenizer import SentenceTokenizer
        from CookieTTS.utils.torchmoji.model_def import torchmoji_feature_encoding
        from CookieTTS.utils.torchmoji.global_variables import PRETRAINED_PATH, VOCAB_PATH
        
        print('Tokenizing using dictionary from {}'.format(VOCAB_PATH))
        with open(VOCAB_PATH, 'r') as f:
            vocabulary = json.load(f)
        
        maxlen = 130
        texts = ["Testing!",]
        
        with torch.no_grad():
            # init model
            st = SentenceTokenizer(vocabulary, maxlen)
            torchmoji = torchmoji_feature_encoding(PRETRAINED_PATH)
        return st, torchmoji
    
    
    def get_torchmoji_hidden(self, texts):
        with torch.no_grad():
            tokenized, _, _ = self.tm_sentence_tokenizer.tokenize_sentences(texts) # input array [B] e.g: ["Test?","2nd Sentence!"]
            embedding = self.tm_torchmoji(tokenized) # returns np array [B, Embed]
        return embedding
    
    
    def load_hifigan(self, vocoder_path):
        print("Loading HiFi-GAN...")
        from CookieTTS._4_mtw.hifigan.models import load_model as load_hifigan_model
        vocoder, vocoder_config = load_hifigan_model(vocoder_path)
        self.hconf = os.path.join(os.path.dirname(vocoder_path), 'config.json')
        print("Done!")
        
        print("Clearing CUDA Cache... ", end='')
        torch.cuda.empty_cache()
        print("Done!")
        
        print('\n'*10)
        import gc # prints currently alive Tensors and Variables  # - And fixes the memory leak? I guess poking the leak with a stick is the answer for now.
        for obj in gc.get_objects():
            try:
                if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                    pass#print(type(obj), obj.size())
            except:
                pass
        print('\n'*10)
        
       
        return vocoder, vocoder_config
        
    
    
    def update_tacotron2_hparams(self, hparams):
        if not hasattr(hparams, 'LL_SpectLoss'):
            hparams.LL_SpectLoss = False
        if not hasattr(hparams, 'use_memory_bottleneck'):
            hparams.use_memory_bottleneck = False
        if not hasattr(hparams, 'prenet_noise'):
            hparams.prenet_noise = 0.0
        return hparams
    
    
    def load_tacotron2(self, tacotron_path):
        """Loads tacotron2,
        Returns:
        - model
        - hparams
        - speaker_lookup
        """
        checkpoint = torch.load(tacotron_path) # load file into memory
        print("Loading Tacotron... ", end="")
        checkpoint_hparams = self.update_tacotron2_hparams(checkpoint['hparams']) # get hparams
        checkpoint_dict = checkpoint['state_dict'] # get state_dict
        
        model = load_model(checkpoint_hparams) # initialize the model
        model.load_state_dict(checkpoint_dict) # load pretrained weights
        _ = model.cuda().eval()#.half()
        
        
        
        print("Done")
        
        #print("Compiling Tacotron Decoder... ", end='')
        #model.decoder = torch.jit.script(model.decoder)
        #print("Done")
        
        tacotron_speaker_name_lookup = checkpoint['speaker_name_lookup'] # save speaker name lookup
        tacotron_speaker_id_lookup = checkpoint['speaker_id_lookup'] # save speaker_id lookup
        print(f"This Tacotron model has been trained for {checkpoint['iteration']} Iterations.")
        return model, checkpoint_hparams, tacotron_speaker_name_lookup, tacotron_speaker_id_lookup
    
    
    def update_tt(self, tacotron_name):
        self.tacotron, self.ttm_hparams, self.ttm_sp_name_lookup, self.ttm_sp_id_lookup = self.load_tacotron2(self.conf['TTM']['models'][tacotron_name]['modelpath'])
        self.ttm_current = tacotron_name
        
        if self.conf['TTM']['use_speaker_ids_file_override']:# (optional) override
            self.ttm_sp_name_lookup = {name: self.ttm_sp_id_lookup[int(ext_id)] for _, name, ext_id in load_filepaths_and_text(self.conf['TTM']['speaker_ids_file'])}
    
    
    def get_closest_names(self, names):
        possible_names = list(self.ttm_sp_name_lookup.keys())
        validated_names = [difflib.get_close_matches(name, possible_names, n=2, cutoff=0.01)[0] for name in names] # change all names in input to the closest valid name
        return validated_names
    
    
    @torch.no_grad()
    def infer(self, text, speaker_names, style_mode, textseg_mode, batch_mode, max_attempts, max_duration_s, batch_size, dyna_max_duration_s, use_arpabet, target_score, speaker_mode, cat_silence_s, textseg_len_target, denoise1, srpower, skipfilter ,gate_delay=3, gate_threshold=0.6, filename_prefix=None, status_updates=True, show_time_to_gen=True, end_mode='max', absolute_maximum_tries=2048, absolutely_required_score=-1e3):
        """
        PARAMS:
        ...
        gate_delay
            default: 4
            options: int ( 0 -> inf )
            info: a modifier for when spectrograms are cut off.
                  This would allow you to add silence to the end of a clip without an unnatural fade-out.
                  8 will give 0.1 seconds of delay before ending the clip.
                  If this param is set too high then the model will try to start speaking again
                  despite not having any text left to speak, therefore keeping it low is typical.
        gate_threshold
            default: 0.7
            options: float ( 0.0 -> 1.0 )
            info: used to control when Tacotron2 will stop generating new mel frames.
                  This will effect speed of generation as the model will generate
                  extra frames till it hits the threshold. This may be preferred if
                  you believe the model is stopping generation too early.
                  When end_mode == 'thresh', this param will also be used to decide
                  when the audio from the best spectrograms should be cut off.
        ...
        end_mode
            default: 'thresh'
            options: ['max','thresh']
            info: controls where the spectrograms are cut off.
                  'max' will cut the spectrograms off at the highest gate output, 
                  'thresh' will cut off spectrograms at the first gate output over gate_threshold.
        """
        assert end_mode in ['max','thresh'], f"end_mode of {end_mode} is not valid."
        assert gate_delay > -10, "gate_delay is negative."
        assert gate_threshold >  0.0, "gate_threshold less than 0.0"
        assert gate_threshold <= 1.0, "gate_threshold greater than 1.0"
        os.makedirs(self.conf["working_directory"], exist_ok=True)
        os.makedirs(self.conf["output_directory" ], exist_ok=True)
        
        # time to gen
        audio_len = 0
        start_time = time.time()
        
        # Score Metric
        scores = []
        
        # Score Parameters
        diagonality_weighting = 0.8 # 'pacing factor', a penalty for clips where the model pace changes often/rapidly. # this thing does NOT work well for Rarity.
        max_focus_weighting = 0.7   # 'stuck factor', a penalty for clips that spend execisve time on the same letter.
        min_focus_weighting = .6   # 'miniskip factor', a penalty for skipping/ignoring single letters in the input text.
        avg_focus_weighting = .5   # 'skip factor', a penalty for skipping very large parts of the input text
        
        # add a filename prefix to keep multiple requests seperate
        if not filename_prefix:
            filename_prefix = f'{time.time():.2f}'
        
        # add output filename
        output_filename = f"{filename_prefix}_output"
        
        # split the text into chunks (if applicable)
        if textseg_mode == 'no_segmentation':
            texts = [text,]
        elif textseg_mode == 'segment_by_line':
            texts = text.split("\n")
        elif textseg_mode == 'segment_by_sentence':
            texts = parse_text_into_segments(text, split_at_quotes=False, target_segment_length=textseg_len_target)
        elif textseg_mode == 'segment_by_sentencequote':
            texts = parse_text_into_segments(text, split_at_quotes=True, target_segment_length=textseg_len_target)
        else:
            raise NotImplementedError(f"textseg_mode of {textseg_mode} is invalid.")
        del text
        
        # cleanup for empty inputs.
        texts = [x.strip() for x in texts if len(x.strip())]
        
        total_len = len(texts)
        
        # update Tacotron stopping params
        frames_per_second = float(self.ttm_hparams.sampling_rate/self.ttm_hparams.hop_length)
        self.tacotron.decoder.gate_delay = int(gate_delay)
        self.tacotron.decoder.max_decoder_steps = int(min(max([len(t) for t in texts]) * float(dyna_max_duration_s)*frames_per_second, float(max_duration_s)*frames_per_second))
        self.tacotron.decoder.gate_threshold = float(gate_threshold)
        
        # find closest valid name(s)
        speaker_names = self.get_closest_names(speaker_names)
        
        # pick how the batch will be handled
        batch_size = int(batch_size)
        if batch_mode == "scaleup":
            simultaneous_texts = total_len
            batch_size_per_text = batch_size
        elif batch_mode == "nochange":
            simultaneous_texts = max(batch_size//max_attempts, 1)
            batch_size_per_text = min(batch_size, max_attempts)
        elif batch_mode == "scaledown":
            simultaneous_texts = total_len
            batch_size_per_text = -(-batch_size//total_len)
        else:
            raise NotImplementedError(f"batch_mode of {batch_mode} is invalid.")
        
        # for size merging
        running_fsize = 0
        fpaths = []
        out_count = 0
        
        # keeping track of stats for html/terminal
        show_inference_progress_start = time.time()
        all_best_scores = []
        continue_from = 0
        counter = 0
        total_specs = 0
        n_passes = 0
        
        text_batch_in_progress = []
        for text_index, text in enumerate(texts):
            if text_index < continue_from: print(f"Skipping {text_index}.\t",end=""); counter+=1; continue
            last_text = (text_index == (total_len-1)) # true if final text input
            
            # setup the text batches
            text_batch_in_progress.append(text)
            if (len(text_batch_in_progress) == simultaneous_texts) or last_text: # if text batch ready or final input
                text_batch = text_batch_in_progress
                text_batch_in_progress = []
            else:
                continue # if batch not ready, add another text
            
            self.tacotron.decoder.max_decoder_steps = int(min(max([len(t) for t in text_batch]) * float(dyna_max_duration_s)*frames_per_second, float(max_duration_s)*frames_per_second))
            
            if speaker_mode == "not_interleaved": # non-interleaved
                batch_speaker_names = speaker_names * -(-simultaneous_texts//len(speaker_names))
                batch_speaker_names = batch_speaker_names[:simultaneous_texts]
            elif speaker_mode == "interleaved": # interleaved
                repeats = -(-simultaneous_texts//len(speaker_names))
                batch_speaker_names = [i for i in speaker_names for _ in range(repeats)][:simultaneous_texts]
            elif speaker_mode == "random": # random
                batch_speaker_names = [random.choice(speaker_names),] * simultaneous_texts
            elif speaker_mode == "cycle_next": # use next speaker for each text input
                def shuffle_and_return():
                    first_speaker = speaker_names[0]
                    speaker_names.append(speaker_names.pop(0))
                    return first_speaker
                batch_speaker_names = [shuffle_and_return() for i in range(simultaneous_texts)]
            elif speaker_mode == "single":
                batch_speaker_names = [speaker_names[0]] * simultaneous_texts  # Use only the first 
            else:
                raise NotImplementedError
            
            if 0:# (optional) use different speaker list for text inside quotes
                speaker_ids = [random.choice(speakers).split("|")[2] if ('"' in text) else random.choice(narrators).split("|")[2] for text in text_batch] # pick speaker if quotemark in text, else narrator
            text_batch  = [text.replace('"',"") for text in text_batch] # remove quotes from text
            
            if len(batch_speaker_names) > len(text_batch):
                batch_speaker_names = batch_speaker_names[:len(text_batch)]
                simultaneous_texts = len(text_batch)
            
            # get speaker_ids (tacotron)
            tacotron_speaker_ids = [self.ttm_sp_name_lookup[speaker] for speaker in batch_speaker_names]
            tacotron_speaker_ids = torch.LongTensor(tacotron_speaker_ids).cuda().repeat_interleave(batch_size_per_text)
            
            # get style input
            if style_mode == 'mel':
                mel = load_mel(audio_path.replace(".npy",".wav")).cuda().half()
                style_input = mel
            elif style_mode == 'token':
                pass
                #style_input =
            elif style_mode == 'zeros':
                style_input = None
            elif style_mode == 'torchmoji_hidden':
                try:
                    tokenized, _, _ = self.tm_sentence_tokenizer.tokenize_sentences(text_batch) # input array [B] e.g: ["Test?","2nd Sentence!"]
                except:
                    raise Exception(f"TorchMoji failed to tokenize text:\n{text_batch}")
                try:
                    embedding = self.tm_torchmoji(tokenized) # returns np array [B, Embed]
                except Exception as ex:
                    print(f'Exception: {ex}')
                    print(f"TorchMoji failed to process text:\n{text_batch}")
                    #raise Exception(f"text\n{text}\nfailed to process.")
                style_input = torch.from_numpy(embedding).cuda().repeat_interleave(batch_size_per_text, dim=0)
                style_input = style_input.to(next(self.tacotron.parameters()).dtype)
            elif style_mode == 'torchmoji_string':
                style_input = text_batch
                raise NotImplementedError
            else:
                raise NotImplementedError
            
            if style_input.size(0) < (simultaneous_texts*batch_size_per_text):
                diff = -(-(simultaneous_texts*batch_size_per_text) // style_input.size(0))
                style_input = style_input.repeat(diff, 1)[:simultaneous_texts*batch_size_per_text]
            
            # check punctuation and add '.' if missing
            valid_last_char = '-,.?!;:' # valid final characters in texts
            text_batch = [text+'.' if (text[-1] not in valid_last_char) else text for text in text_batch]
            
            # parse text
            text_batch = [unidecode(text.replace("...",". ").replace(". . ",". ").replace("  "," ").strip().lstrip('. ')) for text in text_batch] # remove eclipses, double spaces, unicode and spaces before/after the text.
            gtext_batch = text_batch
            if use_arpabet: # convert texts to ARPAbet (phonetic) versions.
                text_batch = [self.ARPA(text) for text in text_batch]
            
            # convert texts to number representation, pad where appropriate and move to GPU
            sequence_split = [torch.LongTensor(text_to_sequence(text, self.ttm_hparams.text_cleaners)) for text in text_batch] # convert texts to numpy representation
            text_lengths = torch.tensor([seq.size(0) for seq in sequence_split])
            max_len = text_lengths.max().item()
            sequence = torch.zeros(text_lengths.size(0), max_len).long() # create large tensor to move each text input into
            for i in range(text_lengths.size(0)): # move each text into padded input tensor
                sequence[i, :sequence_split[i].size(0)] = sequence_split[i]
            sequence = sequence.cuda().long().repeat_interleave(batch_size_per_text, dim=0) # move to GPU and repeat text
            text_lengths = text_lengths.cuda().long() # move to GPU
            
            # debug # Looks like pytorch 1.5 doesn't run contiguous on some operations the previous versions did.
            text_lengths = text_lengths.clone()
            sequence = sequence.clone()
            
            print("tacotron2 batchsize =",sequence.shape[0]) # debug
            
            if status_updates: tt_start=time.time(); print("Running Tacotron2... ")
            try:
                best_score = np.ones(simultaneous_texts) * -1e5
                tries      = np.zeros(simultaneous_texts)
                best_generations = [0]*simultaneous_texts
                best_score_str = ['']*simultaneous_texts
                while np.amin(best_score) < target_score:
                    # run Tacotron
                    outputs = self.tacotron.inference(sequence, text_lengths.repeat_interleave(batch_size_per_text, dim=0), tacotron_speaker_ids, style_input)
                    mel_batch_outputs_postnet = outputs['pred_mel_postnet']
                    gate_batch_outputs        = outputs['pred_gate']
                    alignments_batch          = outputs['alignments']
                    
                    # metric for html side
                    n_passes+=1 # metric for html
                    total_specs+=mel_batch_outputs_postnet.shape[0]
                    
                    # get alignment metrics for each item
                    if end_mode == 'thresh':
                        output_lengths = get_first_over_thresh(gate_batch_outputs, gate_threshold)
                    elif end_mode == 'max':
                        output_lengths = gate_batch_outputs.argmax(dim=1)
                    atd = alignment_metric(alignments_batch, input_lengths=text_lengths.repeat_interleave(batch_size_per_text, dim=0), output_lengths=output_lengths)
                    
                    diagonality_batch   = atd['diagonalitys']
                    avg_prob_batch      = atd['avg_prob']
                    enc_max_dur_batch   = atd['encoder_max_focus']
                    enc_min_dur_batch   = atd['encoder_min_focus']
                    enc_avg_dur_batch   = atd['encoder_avg_focus']
                    p_missing_enc_batch = atd['p_missing_enc']
                    
                    # split batch into items
                    batch = list(zip(
                        output_lengths.split(1, dim=0),
                        mel_batch_outputs_postnet.split(1,dim=0),
                        gate_batch_outputs.split(1,dim=0),
                        alignments_batch.split(1,dim=0),
                        diagonality_batch,
                        avg_prob_batch,
                        enc_max_dur_batch,
                        enc_min_dur_batch,
                        enc_avg_dur_batch,
                        p_missing_enc_batch,))
                    
                    for j in range(simultaneous_texts): # process each set of text spectrograms seperately
                        start, end = (j*batch_size_per_text), ((j+1)*batch_size_per_text)
                        sametext_batch = batch[start:end] # seperate the full batch into pieces that use the same input text
                        assert len(sametext_batch) >= 1
                        
                        # process all items related to the j'th text input
                        for k, (output_length, mel_outputs_postnet, gate_outputs, alignments, diagonality, avg_prob, enc_max_focus, enc_min_focus, enc_avg_focus, p_missing_enc) in enumerate(sametext_batch):
                            # factors that make up score
                            weighted_score = avg_prob.item() # general alignment quality
                            diagonality_punishment = (max(diagonality.item(),1.10)-1.10) * 0.5 * diagonality_weighting  # speaking each letter at a similar pace.
                            max_dur_punishment = max((enc_max_focus.item()-60), 0) * 0.005 * max_focus_weighting # getting stuck on same letter for 0.5s
                            min_dur_punishment = max(0.00-enc_min_focus.item(),0) * min_focus_weighting # skipping single enc outputs
                            avg_dur_punishment = max(3.6-enc_avg_focus.item(), 0) * avg_focus_weighting # skipping most enc outputs
                            mis_dur_punishment = max(p_missing_enc.item()-0.08, 0) if text_lengths[j] > 12 else 0.0 # skipping some percent of the text
                            
                            weighted_score -= (diagonality_punishment + max_dur_punishment + min_dur_punishment + avg_dur_punishment + mis_dur_punishment)
                            score_str = (f"[{weighted_score:.3f}weighted_score] "
                                         f"[{diagonality.item():>5.3f} diagonality] "
                                         f"[{avg_prob.item():>06.2%}avg_max_att] "
                                         f"[{diagonality_punishment:>5.2f}diagonality_punishment] "
                                         f"[{max_dur_punishment:>5.2f}max_dur_punishment] "
                                         f"[{min_dur_punishment:>5.2f}min_dur_punishment] "
                                         f"[{avg_dur_punishment:>5.2f}avg_dur_punishment] "
                                         f"[{mis_dur_punishment:>5.2f}mis_dur_punishment]|")
                            if torch.isnan(mel_outputs_postnet).any() or torch.isnan(gate_outputs).any() or torch.isnan(alignments).any() or weighted_score == float('nan'):
                                weighted_score = 1e-7
                            if weighted_score > best_score[j]:
                                best_score[j] = weighted_score
                                best_score_str[j] = score_str
                                best_generations[j] = [mel_outputs_postnet, output_length, alignments]
                            tries[j]+=1
                            if np.amin(tries) >= max_attempts and np.amin(best_score) > (absolutely_required_score-1):
                                raise StopIteration
                            if np.amin(tries) >= absolute_maximum_tries:
                                print(f"Absolutely required score not achieved in {absolute_maximum_tries} attempts - ", end='')
                                raise StopIteration
                    
                    if np.amin(tries) < (max_attempts-1):
                        print(f'Minimum score of {np.amin(best_score)} is less than Target score of {target_score}. Retrying.')
                    elif np.amin(best_score) < absolutely_required_score:
                        print(f"Minimum score of {np.amin(best_score)} is less than 'Absolutely Required score' of {absolutely_required_score}. Retrying.")
            except StopIteration:
                del batch
                if status_updates: print(f"Done in {time.time()-tt_start:.2f}s")
                pass
            
            assert not any([x == 0 for x in best_generations]), 'Tacotron Failed to generate one of the texts after multiple attempts.'
            
            # logging
            all_best_scores.extend(best_score)
            
            # cleanup VRAM
            style_input = sequence = None
            
            # [[mel, melpost, gate, align], [mel, melpost, gate, align], [mel, melpost, gate, align]] -> [[mel, mel, mel], [melpost, melpost, melpost], [gate, gate, gate], [align, align, align]]
            mel_batch_outputs_postnet = [x[0][0].T for x in best_generations]
            output_lengths            = torch.stack([x[1][0] for x in best_generations], dim=0)
            #alignments_batch          = [x[2][0] for x in best_generations]
            # pickup the best attempts from each input
            
            max_length = output_lengths.max()
            mel_batch_outputs_postnet = torch.nn.utils.rnn.pad_sequence(mel_batch_outputs_postnet, batch_first=True, padding_value=-11.52).transpose(1,2)[:,:,:max_length]
            alignments_batch = torch.nn.utils.rnn.pad_sequence(alignments_batch, batch_first=True, padding_value=0)[:,:max_length,:]
            '''
            mel_batch_outputs_postnet  = mel_batch_outputs_postnet.float()  # Ensure correct dtype
            mel_80 = mel_batch_outputs_postnet
            mel_80_transposed = mel_80.transpose(1, 2)  # [batch, time, 80]
            mel_160_transposed = torch.nn.functional.interpolate(mel_80_transposed, size=160, mode='linear', align_corners=True)
            mel_160 = mel_160_transposed.transpose(1, 2)  # [batch, 160, time]
            mel_batch_outputs_postnet = mel_160
            '''
            
            if status_updates:
                vo_start = time.time()
                print("Running Vocoder... ")
            
            self.vocoder_batch_size = 16
            
            # Run Vocoder
            vocoder_dtype = next(self.vocoder.parameters()).dtype
            audio_batch = []
            for i in range(0, len(mel_batch_outputs_postnet), self.vocoder_batch_size):
                pred_mel_part_batch = mel_batch_outputs_postnet[i:i+self.vocoder_batch_size]
                audio_batch.extend( self.vocoder(pred_mel_part_batch.to(vocoder_dtype)).squeeze(1).cpu().split(1, dim=0) )# [b, T]
            # audio_batch shapes = [[1, T], [1, T], [1, T], [1, T], ...]
            
            if status_updates:
                print(f'Done in {time.time()-vo_start:.2f}s')
            
            if status_updates: sv_start=time.time(); print(f"Saving audio files to disk... ")
            
            # write audio files and any stats
            audio_bs = len(audio_batch)
            for j, audio in enumerate(audio_batch):
                # remove Vocoder padding
                audio_end = output_lengths[j] * self.ttm_hparams.hop_length
                audio = audio[:,:audio_end]
                
                # remove Tacotron2 padding
                spec_end = output_lengths[j]
                mel_outputs_postnet = mel_batch_outputs_postnet.split(1, dim=0)[j][:,:,:spec_end]
                alignments = alignments_batch.split(1, dim=0)[j][:,:spec_end,:text_lengths[j]]
                
                # save audio
                filename = f"{filename_prefix}_{counter//300:04}_{counter:06}.wav"
                save_path = os.path.join(self.conf['working_directory'], filename)
                
                # add silence to clips (ignore last clip)
                if cat_silence_s:
                    cat_silence_samples = int(cat_silence_s*self.ttm_hparams.sampling_rate)
                    audio = torch.nn.functional.pad(audio, (0, cat_silence_samples))
                

                # Hifi-Gan Denoiser
                with open(self.hconf) as f:
                    json_config = json.loads(f.read())
                h = AttrDict(json_config)
                hifigan_a = Generator(h).to(torch.device("cuda"))
                denoiser = Denoiser(hifigan_a, mode="normal")
                fs = h.sampling_rate  # Sampling rate from config

                # Denoise audio
                print("demoising audio...")
                audio = audio * MAX_WAV_VALUE
                audio_denoised = denoiser(audio.view(1, -1), strength=denoise1)[:, 0]  # Reduced strength
                audio_np = audio_denoised.cpu().numpy().astype(np.float64)
                if np.any(np.isnan(audio_np)) or np.any(np.isinf(audio_np)):
                    raise ValueError("NaN or Inf detected in audio_np after denoising")

                # Normalize
                max_amplitude = np.max(np.abs(audio_np))
                normalize = (MAX_WAV_VALUE / max_amplitude) ** 0.9
                if max_amplitude > 0:
                    print(f"Normalizing audio with max amplitude {max_amplitude:.2f}")
                    audio_np *= normalize
                    
                
                # Optional high-pass filter (use lower cutoff for speech)
                # Comment out to test without filtering
                
                b = scipy.signal.firwin(numtaps=101, cutoff=60, fs=fs, pass_zero=False)
                audio_filtered = scipy.signal.lfilter(b, [1.0], audio_np)
                mix_factor = .4  # Blend filtered and unfiltered audio
                audio_mixed = (1 - mix_factor) * audio_np + mix_factor * audio_filtered
                if skipfilter:
                    audio_mixed = audio_np  # Uncomment to skip filter entirely

                # Moderate amplification
                audio_mixed *= srpower  # Adjust as needed (1.0 for no extra gain)
                audio_mixed /= normalize  # Denormalize
               
                # Clip to prevent overflow
                audio_mixed = np.clip(audio_mixed, -MAX_WAV_VALUE, MAX_WAV_VALUE)

                # Scale to int16
                audio = (audio_mixed * 32767 / MAX_WAV_VALUE).astype(np.int16)
            
                # Save audio
                filename = f"{filename_prefix}_{counter//300:04}_{counter:06}.wav"
                save_path = os.path.join(self.conf['working_directory'], filename)
                if os.path.exists(save_path):
                    print(f"File already found at [{save_path}], overwriting.")
                    os.remove(save_path)
                audio = audio.flatten()
                
                write(save_path, fs, audio)
                        
                
                counter+=1
                audio_len+=audio_end
                
                # ------ merge clips of 300 ------ #
                last_item = (j == audio_bs-1)
                if (counter % 300) == 0 or (last_text and last_item): # if 300th file or last item of last batch.
                    i = (counter- 1)//300
                    # merge batch of 300 files together
                    print(f"Merging audio files {i*300} to {((i+1)*300)-1}... ", end='')
                    fpath = os.path.join(self.conf['working_directory'], f"{filename_prefix}_concat_{i:04}.wav")
                    files_to_merge = os.path.join(self.conf["working_directory"], f"{filename_prefix}_{i:04}_*.wav")
                    os.system(f'sox "{files_to_merge}" -b 16 "{fpath}"')
                    assert os.path.exists(fpath), f"'{fpath}' failed to generate."
                    del files_to_merge
                    
                    # delete the original 300 files
                    print("Cleaning up remaining temp files... ", end="")
                    tmp_files = [fp for fp in glob(os.path.join(self.conf['working_directory'], f"{filename_prefix}_{i:04}_*.wav")) if "output" not in fp]
                    _ = [os.remove(fp) for fp in tmp_files]
                    print("Done")
                    
                    # add merged file to final output(s)
                    fsize = os.stat(fpath).st_size
                    running_fsize += fsize
                    fpaths += [fpath,]
                    if ( running_fsize/(1024**3) > self.conf['output_maxsize_gb'] ) or (len(fpaths) > 300) or (last_text and last_item): # if (total size of fpaths is > 2GB) or (more than 300 inputs) or (last item of last batch): save as output
                        fpath_str = '"'+'" "'.join(fpaths)+'"' # chain together fpaths in string for SoX input
                        output_extension = self.conf['sox_output_ext']
                        if output_extension[0] != '.':
                            output_extension = f".{output_extension}"
                        out_name = f"{output_filename}_{out_count:02}{output_extension}"
                        out_path = os.path.join(self.conf['output_directory'], out_name)
                        os.system(f'sox {fpath_str} -b 16 "{out_path}"') # merge the merged files into final outputs. bit depth of 16 useful to stay in the 32bit duration limit
                        
                        if running_fsize >= (os.stat(out_path).st_size - 1024): # if output seems to have correctly generated.
                            print("Cleaning up merged temp files... ", end="") # delete the temp files and keep the output
                            _ = [os.remove(fp) for fp in fpaths]
                            print("Done")
                        
                        running_fsize = 0
                        out_count+=1
                        fpaths = []
                # ------ // merge clips of 300 // ------ #
                #end of writing loop
            
            if status_updates: print(f"Done in {time.time()-sv_start:.2f}s")
            
            show_inference_alignment_scores = True# Needs to be moved, workers cannot print to terminal. #bool(self.conf['terminal']['show_inference_alignment_scores'])
            for k, score in enumerate(best_score):
                scores+=[score,]
                if show_inference_alignment_scores:
                    print(f'Input_Str {k:02}: "{gtext_batch[k]}"\n'
                          f'Score_Str {k:02}: {best_score_str[k]}\n')
            
            if True:#self.conf['terminal']['show_inference_progress']:
                time_elapsed = time.time()-show_inference_progress_start
                time_per_clip = time_elapsed/(text_index+1)
                remaining_files = (total_len-(text_index+1))
                eta_finish = (remaining_files*time_per_clip)/60
                print(f"{text_index}/{total_len}, {eta_finish:.2f}mins remaining.")
                del time_per_clip, eta_finish, remaining_files, time_elapsed
            
            audio_seconds_generated = round(audio_len.item()/self.ttm_hparams.sampling_rate,3)
            time_to_gen = round(time.time()-start_time,3)
            if show_time_to_gen:
                print(f"Generated {audio_seconds_generated}s of audio in {time_to_gen}s wall time - so far. (best of {tries.sum().astype('int')} tries this pass) ({audio_seconds_generated/time_to_gen:.2f}xRT) ({sum([x<0.6 for x in all_best_scores])/len(all_best_scores):.1%}Failure Rate)")
            
            print("\n") # seperate each pass
        
        scores = np.stack(scores)
        avg_score = np.mean(scores)
        
        return out_name, time_to_gen, audio_seconds_generated, total_specs, n_passes, avg_score


def start_worker(config, device, request_queue, finished_queue):
    """
    Start a Text-2-Speech Worker.
    This process will check request_queue for requests and complete them till the host process is killed or something crashes.
    """
    t2s = T2S(conf['workers']) # initialize Text-2-Speech module. Loads models
    
    while queue_is_empty:
        time.sleep(0.1)# wait 100ms then check queue again...
