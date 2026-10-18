# facedoor cloud-init meta-data (NoCloud). A new instance-id per staging makes cloud-init treat a
# re-flashed or re-staged card as a new instance and apply user-data again.
instance-id: {{INSTANCE_ID}}
local-hostname: {{HOSTNAME}}
