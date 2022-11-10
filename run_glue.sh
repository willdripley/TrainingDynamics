# ====================== Basic GLUE tasks training and evaluation:
# the following is the standard training for GLUE tasks

# export TASK_NAME=sst2
# export MODEL=distilbert-base-cased
# python -m torch.distributed.launch --nproc_per_node 8 --use_env run_glue.py \
#   --model_name_or_path $MODEL \
#   --task_name $TASK_NAME \
#   --max_length 128 \
#   --per_device_train_batch_size 32 \
#   --learning_rate 2e-5 \
#   --num_train_epochs 5 \


# ————> Training with Data Selection: 
# after you run `data_selection.py` and obtain the `three_regions_data_indices.json` file
# you can train a GLUE classifier again with your specified data selection
# set `--with_data_selection` to turn on data selection
# set `--data_selection_region [region]` to specify the region, choices are "easy", "hard", and "ambiguous"


# ————> Suggested models: 
# prajjwal1/bert-tiny
# distilbert-base-cased
# bert-base-cased
# roberta-large

# =========== Train GLUE tasks =============
# https://huggingface.co/datasets/glue

export TASK_NAME=mnli
export MODEL=google/electra-small-discriminator
# CUDA_VISIBLE_DEVICES=4 python run_glue.py \
python -m torch.distributed.launch --nproc_per_node 8 --use_env run_glue.py \
  --seed 5 \
  --model_name_or_path $MODEL \
  --task_name $TASK_NAME \
  --checkpointing_steps epoch \
  # --resume_from_checkpoint saved_models/$TASK_NAME/$MODEL/epoch_4 \
  --max_length 128 \
  --per_device_train_batch_size 32 \
  --learning_rate 2e-5 \
  --num_train_epochs 3 \
  --log_name ambiguous \
  --output_dir saved_models/$TASK_NAME/$MODEL
  # --continue_train_with_sample_loss \
  # --continue_train \
  # --continue_num_train_epochs 5  \
  # --selected_indices_filename selected_indices_ambi_top0.1_balance+ \
  # --do_lwf \
  
  

  # --output_dir saved_models/$TASK_NAME/$MODEL \
  # --resume_from_checkpoint saved_models/$TASK_NAME/$MODEL/epoch_4 \  # 指定了之后，就会直接load该epoch的模型

  # --max_train_steps 10 \
  # --with_data_selection \
  # --data_selection_region ambiguous \
  # --output_dir tmp/$TASK_NAME/


# =========== Use Your Own Dataset ========

# export MODEL=bert-base-cased
# python -m torch.distributed.launch --nproc_per_node 8 --use_env run_glue.py \
#   --model_name_or_path $MODEL \
#   --max_length 128 \
#   --per_device_train_batch_size 32 \
#   --learning_rate 2e-5 \
#   --num_train_epochs 5 \
#   --train_file datasets/qnli-easy-hard_train.csv \
#   --validation_file datasets/qnli-easy-hard_valid.csv



# cd ../K2T
# sh oc.sh