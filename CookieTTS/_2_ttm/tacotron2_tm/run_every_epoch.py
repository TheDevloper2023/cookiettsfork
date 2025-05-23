current_iteration = iteration
######################################################################################
##                                                                                  ##
## ████████╗ █████╗  ██████╗ ██████╗ ████████╗██████╗  ██████╗ ███╗   ██╗  ██████╗  ##
## ╚══██╔══╝██╔══██╗██╔════╝██╔═══██╗╚══██╔══╝██╔══██╗██╔═══██╗████╗  ██║  ╚════██╗ ##
##    ██║   ███████║██║     ██║   ██║   ██║   ██████╔╝██║   ██║██╔██╗ ██║   █████╔╝ ##
##    ██║   ██╔══██║██║     ██║   ██║   ██║   ██╔══██╗██║   ██║██║╚██╗██║  ██╔═══╝  ##
##    ██║   ██║  ██║╚██████╗╚██████╔╝   ██║   ██║  ██║╚██████╔╝██║ ╚████║  ███████╗ ##
##    ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝  ╚══════╝ ##
##                                                                                  ##
######################################################################################
## Tacotron2 ##
###############
param_interval = 5# how often this file is ran
dump_filelosses_interval = 500# how often to update file_losses.cvs
show_live_params = False
LossExplosionThreshold = 1e3 # maximum loss value (which will trigger a restart from latest checkpoint)

custom_lr = True
decrease_lr_on_restart = True # Decrease the Learning Rate on a LossExplosionThreshold exception

# Learning Rate / Optimization
decay_start = 99999999
if   iteration <  10000:
    A_ = 5.000e-4
elif iteration <  50000:
    A_ = 2.000e-4
elif iteration <  80000:
    A_ = 1.000e-4
elif iteration < 110000:
    A_ = 0.500e-4
elif iteration < 140000:
    A_ = 0.250e-4
elif iteration < 170000:
    A_ = 0.125e-4
else:
    A_ = 0.050e-4
B_ = 40000
C_ = 0e-5
min_learning_rate = 1e-6
grad_clip_thresh = 1.0 if iteration > 5000 else 5.0

warmup_start_lr = 0.1e-4
warmup_start = checkpoint_iter + 0
warmup_end   = warmup_start + (A_-warmup_start_lr)*1e5 # warmup will linearly increase LR by 1e-5 each iter till LR hits A_

best_model_margin = 0.01 # training loss margin

validation_interval = 125 if iteration < 2000 else (250 if iteration < 10000 else 100)
checkpoint_interval = 1000#validation_interval

# Loss Scalars (set to None to load from hparams.py)
spec_MSE_weight     = 0.0000
spec_MFSE_weight    = 1.0000
postnet_MSE_weight  = 0.0000
postnet_MFSE_weight = 1.0000
gate_loss_weight    = 1.0000
sylps_kld_weight    = 0.0025# try to hold sylps_kld between 0.5 and 2.0
sylps_MSE_weight    = 0.0100
sylps_MAE_weight    = 0.0010
diag_att_weight     = 0.0500# you only want to use this very shortly as it masks attention problems later into training.
if iteration >  5000:
    diag_att_weight *= 0.1
if iteration > 25000:
    diag_att_weight *= 0.5

# Drop Frame Rate
dfr_warmup_start = 0
dfr_warmup_iters = 10
dfr_max_value    = 0.5
drop_frame_rate = dfr_max_value if dfr_max_value < 0.01 else min(max(current_iteration-dfr_warmup_start,0)/(dfr_warmup_iters*dfr_max_value), dfr_max_value) # linearly increase DFR from 0.0 to 0.2 from iteration 1 to 10001.

# Teacher-forcing Config
p_teacher_forcing  = 1.00
teacher_force_till = 0
val_p_teacher_forcing  = 1.00
val_teacher_force_till = 0

# Misc
n_restarts_override = None
