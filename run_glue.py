# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning a 🤗 Transformers model for sequence classification on GLUE."""
import argparse
from collections import defaultdict
import json
import logging
import math
import os
import random
from pathlib import Path

import datasets
import torch
from torch import nn
from datasets import load_dataset, load_from_disk, load_metric
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from huggingface_hub import Repository
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    PretrainedConfig,
    SchedulerType,
    default_data_collator,
    get_scheduler,
)
# from transformers.utils import get_full_repo_name, send_example_telemetry
from transformers.utils.versions import require_version


logger = get_logger(__name__)

require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/text-classification/requirements.txt")

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),

    "snli": ("premise", "hypothesis"),
    
    "boolq": ("question", "passage"),
    "cb": ("premise", "hypothesis"),
    
    "mrpc-noisy":("sentence1", "sentence2"),
    "rte-noisy":("sentence1", "sentence2"),
    "nli-diag":("premise", "hypothesis")
}


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a text classification task")
    parser.add_argument(
        "--task_name",
        type=str,
        default=None,
        help="The name of the glue task to train on.",
        # choices=list(task_to_keys.keys()),
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=128,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded if `--pad_to_max_lengh` is passed."
        ),
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=True,
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"` and `"comet_ml"`. Use `"all"` (default) to report to all integrations.'
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--ignore_mismatched_sizes",
        action="store_true",
        help="Whether or not to enable to load a pretrained model whose head dimensions are different.",
    )
    parser.add_argument("--do_recording", action="store_true", help="Whether to record the training dynamics.")
    parser.add_argument("--with_data_selection", action="store_true", help="Use only a selected subset of the training data for model training.")
    parser.add_argument("--data_selection_region", default=None, choices=("easy","hard","ambiguous", "all"), 
                         help="Three regions from the dataset cartography: easy, hard and ambiguous")
    parser.add_argument("--data_selection_region_extra", default=None, choices=("easy","hard","ambiguous"))
    parser.add_argument("--enable_proper_noun_featurization", type=bool, default=False)
    parser.add_argument("--data_selection_region_prefix", type=str)
    parser.add_argument("--continue_train", action="store_true")
    parser.add_argument("--continue_num_train_epochs", type=int, default=5)
    parser.add_argument("--log_name", type=str, default=None, help='if set, will create a log file recording the metrics')
    parser.add_argument("--selected_indices_filename", type=str)
    parser.add_argument("--do_lwf", action="store_true")
    parser.add_argument("--train_with_sample_loss", default=None,  action="store_true")
    parser.add_argument("--continue_train_with_sample_loss", default=None, action="store_true")
    parser.add_argument("--nli_diagnostics", type=bool, default=False)
    args = parser.parse_args()

    # Sanity checks
    if args.task_name is None and args.train_file is None and args.validation_file is None:
        raise ValueError("Need either a task name or a training/validation file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["csv", "json"], "`train_file` should be a csv or a json file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in ["csv", "json"], "`validation_file` should be a csv or a json file."

    # if args.push_to_hub:
    #     assert args.output_dir is not None, "Need an `output_dir` to create a repo when `--push_to_hub` is passed."

    return args


def main():
    args = parse_args()
    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    # send_example_telemetry("run_glue_no_trainer", args)

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment
    accelerator = (
        Accelerator(log_with=args.report_to, logging_dir=args.output_dir) if args.with_tracking else Accelerator()
    )
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)


    # creating log filed
    def log_to_file(info=None):
        if args.log_name is not None:
            if accelerator.is_local_main_process:
                os.makedirs(f'{args.output_dir}/log/{args.task_name}', exist_ok=True)
                with open(f'{args.output_dir}/log/{args.task_name}/{args.log_name}.txt', 'a') as log_f:
                    if info is not None:
                        log_f.write(str(info)+'\n')
    
    import datetime
    args_str = ' '.join([k+'='+str(args.__dict__[k]) for k in args.__dict__])
    log_to_file('\n-------------------\n')
    log_to_file(datetime.datetime.now())
    log_to_file("- Key params:")
    log_to_file(args_str)

    def prompt_id_to_idx(examples):
        examples["idx"] = examples["promptID"]
        return examples

    # Get the datasets: you can either provide your own CSV/JSON training and evaluation files (see below)
    # or specify a GLUE benchmark task (the dataset will be downloaded automatically from the datasets Hub).

    # For CSV/JSON files, this script will use as labels the column called 'label' and as pair of sentences the
    # sentences in columns called 'sentence1' and 'sentence2' if such column exists or the first two columns not named
    # label if at least two columns are provided.

    # If the CSVs/JSONs contain only one non-label column, the script does single sentence classification on this
    # single column. You can easily tweak this behavior (see below)

    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if args.task_name is not None and args.task_name != "nli-diag":
        # Downloading and loading a dataset from the hub.
        if args.task_name in ['snli']:
            # raw_datasets = load_dataset(args.task_name)
            raw_datasets = load_from_disk(f"datasets/{args.task_name}/with_idx")
            # snli里包含了一些-1的label，得去掉
            # 跟GLUE不同，SNLI包含了有标签的test set
            # raw_datasets['train'] = raw_datasets['train'].filter(lambda x:x['label']!=-1)
            # raw_datasets['validation'] = raw_datasets['validation'].filter(lambda x:x['label']!=-1)
            # raw_datasets['test'] = raw_datasets['test'].filter(lambda x:x['label']!=-1)
        elif args.task_name in ['boolq', 'cb', 'axb', 'axg']:
            raw_datasets = load_dataset("super_glue", args.task_name)
        elif 'noisy' in args.task_name:
            raw_datasets = load_from_disk(f"datasets/{args.task_name}/with_idx")
        elif args.task_name == "mnli":
            raw_datasets = load_dataset("multi_nli").map(prompt_id_to_idx)
        else:
            raw_datasets = load_dataset("glue", args.task_name)
    else:
        # Loading the dataset from local csv or json file.
        data_files = {}
        if args.train_file is not None:
            data_files["train"] = args.train_file
        if args.validation_file is not None:
            data_files["validation"] = args.validation_file
        extension = (args.train_file if args.train_file is not None else args.validation_file).split(".")[-1]
        raw_datasets = load_dataset(extension, data_files=data_files)
    # See more about loading any type of standard or custom dataset at
    # https://huggingface.co/docs/datasets/loading_datasets.html.





    # Labels
    if args.nli_diagnostics:
        label_list = ["entailment", "neutral", "contradiction"]
        num_labels = 3
        is_regression = False
    elif args.task_name is not None:
        is_regression = args.task_name == "stsb"
        if not is_regression:
            if args.task_name == 'mnli':
                label_list = raw_datasets["validation_matched"].features["label"].names
            else:
                label_list = raw_datasets["validation"].features["label"].names
            num_labels = len(label_list)
        else:
            num_labels = 1
    else:
        # Trying to have good defaults here, don't hesitate to tweak to your needs.
        is_regression = raw_datasets["train"].features["label"].dtype in ["float32", "float64"]
        if is_regression:
            num_labels = 1
        else:
            # A useful fast method:
            # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.unique
            label_list = raw_datasets["validation"].unique("label")
            label_list.sort()  # Let's sort it for determinism
            num_labels = len(label_list)

    # --------------------------- Data Selection: -----------------------------------
    # data selection is ONLY applied on train set
    if args.with_data_selection:
        assert args.data_selection_region is not None, "You much specify `data_selection_region` when using `with_data_selection`"
        model_name = args.model_name_or_path
        if '/' in model_name:
            model_name = model_name.split('/')[-1]
        assert os.path.exists(f'{args.data_selection_region_prefix}/three_regions_data_indices.json'), "Selection indices file not found!"
        with open(f'{args.data_selection_region_prefix}/three_regions_data_indices.json','r') as f:
            three_regions_data_indices = json.loads(f.read())
        selected_indices = []
        if args.data_selection_region == "all":
            # Specially constructed percentages
            selected_indices = three_regions_data_indices["easy"] + three_regions_data_indices["hard"] + three_regions_data_indices["ambiguous"]
        else:
            selected_indices = three_regions_data_indices[args.data_selection_region]
            if args.data_selection_region_extra:
                print('selected_indices len before extra:', len(selected_indices))
                selected_indices += three_regions_data_indices[args.data_selection_region_extra]
                print('selected_indices len after extra:', len(selected_indices))
        raw_datasets['train'] = raw_datasets['train'].select(selected_indices)

        logger.info("~~~~~ Applying Data Selection ~~~~~ ")
        logger.info(f"~~~~~ Region: {args.data_selection_region} ")
        logger.info(f"~~~~~ Size: {len(raw_datasets['train'])} ")
        
    # ----------------------------------------------------------------------------------------------------
    # with open(f'dy_log/sst2/distilbert-base-cased/three_regions_data_indices.json','r') as f:

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(args.model_name_or_path, num_labels=num_labels, finetuning_task=args.task_name)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=not args.use_slow_tokenizer)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
        ignore_mismatched_sizes=args.ignore_mismatched_sizes,
    )
    
    # 对非HF官方模型的名称的处理，只保留模型名
    if '/' in args.model_name_or_path:
        args.model_name_or_path = args.model_name_or_path.split('/')[-1]

    # Preprocessing the datasets
    # --------------- GLUE tasks ---------------
    if args.task_name is not None:
        if 'noisy' in args.task_name:
            task_name = args.task_name.split('-')[0]
            sentence1_key, sentence2_key = task_to_keys[task_name]
        else:
            sentence1_key, sentence2_key = task_to_keys[args.task_name]
    # --------------- Other tasks ---------------
    else:
        # Again, we try to have some nice defaults but don't hesitate to tweak to your use case.
        # 这里的逻辑是这样的：
        # 对于非glue的数据集，要求要包含`label`字段
        # 然后希望你有`sentence1`, `sentence2`这两个字段，这样就跟glue对齐了
        # 如果你也不是用的这个名字，那就选择非label列的前两个字段来分别作为sentence1和sentence2
        non_label_column_names = [name for name in raw_datasets["train"].column_names if name != "label"]
        if "sentence1" in non_label_column_names and "sentence2" in non_label_column_names:
            sentence1_key, sentence2_key = "sentence1", "sentence2"
        elif "sentence" in non_label_column_names:  # for classical classification tasks, like sst2
            sentence1_key, sentence2_key = ("sentence", None)
        elif "question" in non_label_column_names and "sentence" in non_label_column_names: # for tasks like qnli
            sentence1_key, sentence2_key = ("question", "sentence")
        else:
            if len(non_label_column_names) >= 2:
                sentence1_key, sentence2_key = non_label_column_names[:2]
            else:
                sentence1_key, sentence2_key = non_label_column_names[0], None

    # Some models have set the order of the labels to use, so let's make sure we do use it.
    label_to_id = None
    if (
        model.config.label2id != PretrainedConfig(num_labels=num_labels).label2id
        and args.task_name is not None
        and not is_regression
    ):
        # Some have all caps in their config, some don't.
        label_name_to_id = {k.lower(): v for k, v in model.config.label2id.items()}
        if list(sorted(label_name_to_id.keys())) == list(sorted(label_list)):
            logger.info(
                f"The configuration of the model provided the following label correspondence: {label_name_to_id}. "
                "Using it!"
            )
            label_to_id = {i: label_name_to_id[label_list[i]] for i in range(num_labels)}
        else:
            logger.warning(
                "Your model seems to have been trained with labels, but they don't match the dataset: ",
                f"model labels: {list(sorted(label_name_to_id.keys()))}, dataset labels: {list(sorted(label_list))}."
                "\nIgnoring the model labels as a result.",
            )
    elif args.task_name is None and not is_regression:
        label_to_id = {v: i for i, v in enumerate(label_list)}

    if label_to_id is not None:
        model.config.label2id = label_to_id
        model.config.id2label = {id: label for label, id in config.label2id.items()}
    elif args.task_name is not None and not is_regression:
        model.config.label2id = {l: i for i, l in enumerate(label_list)}
        model.config.id2label = {id: label for label, id in config.label2id.items()}

    padding = "max_length" if args.pad_to_max_length else False

    parse_tree_sentence_1 = "premise_parse"
    parse_tree_sentence_2 = "hypothesis_parse"

    def get_proper_nouns(sentence):
        words = sentence.split(' ')
        index = 0
        proper_nouns = set()
        while index < len(words) - 1:
            if index + 1 < len(words):
                first_word = words[index].lstrip("(")
                second_word = words[index + 1].lstrip(")")
                if first_word == "NNP" or first_word == "NNPS":
                    proper_nouns.add(second_word.lower())
            index += 1
        l = list(proper_nouns)
        l.sort()
        return l

    # https://huggingface.co/docs/datasets/process#map
    def proper_noun_features(example):
        sentence_1_proper_nouns = get_proper_nouns(example[parse_tree_sentence_1])
        sentence_2_proper_nouns = get_proper_nouns(example[parse_tree_sentence_2])
        example["sentence_1_proper_nouns"] = str(sentence_1_proper_nouns)
        example["sentence_2_proper_nouns"] = str(sentence_2_proper_nouns)
        return example


    def preprocess_function(examples):
        texts = None
        if args.enable_proper_noun_featurization:
            texts = ((examples[sentence1_key], examples[sentence2_key], examples["sentence_1_proper_nouns"], examples["sentence_2_proper_nouns"]))

        else:
            # Tokenize the texts
            texts = (
                (examples[sentence1_key],) if sentence2_key is None else (examples[sentence1_key], examples[sentence2_key])
            )

        result = tokenizer(*texts, padding=padding, max_length=args.max_length, truncation=True)

        if "label" in examples:
            if label_to_id is not None:
                # Map labels to IDs (not necessary for GLUE tasks)
                result["labels"] = [label_to_id[l] for l in examples["label"]]
            else:
                # In all cases, rename the column to labels because the model will expect that.
                result["labels"] = examples["label"]
        return result

    with accelerator.main_process_first():
        if args.enable_proper_noun_featurization:
            raw_datasets = raw_datasets.map(proper_noun_features)
            # print("asdf", raw_datasets["train"]["sentence_1_proper_nouns"][1:100])
        processed_datasets = raw_datasets.map(
            preprocess_function,
            batched=True,
            # 得把这行改掉：
            # 以SST2为例，这里会把 ['sentence', 'label', 'idx'] 给去掉（不用担心label，因为上面已经新建了一个labels列）
            # remove_columns=raw_datasets["train"].column_names,  
            # 改为：
            remove_columns=[c for c in raw_datasets["train"].column_names if c != 'idx'],  # 保留idx，其他的可以去掉
            desc="Running tokenizer on dataset",
        )

    train_dataset = processed_datasets["train"]
    
    # ============================------------------------------
    # 8.29 MNLI add reverse pair data
    if False:
        with open('HCT/mnli_easy_label2_top10k.txt','r') as f:
            selected_ids = [int(x) for x in f.readlines()]
        train_data = raw_datasets['train'].select(selected_ids)
        orig_premise_list = train_data['premise']
        orig_hypothesis_list = train_data['hypothesis']
        new_train_data = train_data.remove_columns(['hypothesis','premise']).add_column('premise', orig_hypothesis_list).add_column('hypothesis', orig_premise_list)
        
        with accelerator.main_process_first():
            processed_new_train_data = new_train_data.map(
                preprocess_function,
                batched=True,
                remove_columns=[c for c in raw_datasets["train"].column_names if c != 'idx'],  # 保留idx，其他的可以去掉
                desc="Running tokenizer on dataset",
            )
        from datasets import concatenate_datasets
        train_dataset = concatenate_datasets([train_dataset,processed_new_train_data])
    # ============================------------------------------


    eval_dataset = processed_datasets["validation_matched" if args.task_name == "mnli" else "validation"]

    # Log a few random samples from the training set:
    for index in random.sample(range(len(train_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # DataLoaders creation:
    if args.pad_to_max_length:
        # If padding was already done ot max length, we use the default data collator that will just convert everything
        # to tensors.
        data_collator = default_data_collator
    else:
        # Otherwise, `DataCollatorWithPadding` will apply dynamic padding for us (by padding to the maximum length of
        # the samples passed). When using mixed precision, we add `pad_to_multiple_of=8` to pad all tensors to multiple
        # of 8s, which will enable the use of Tensor Cores on NVIDIA hardware with compute capability >= 7.5 (Volta).
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=(8 if accelerator.use_fp16 else None))

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        # num_warmup_steps=args.num_warmup_steps,
        num_warmup_steps=int(args.max_train_steps * 0.06),
        num_training_steps=args.max_train_steps,
    )

    # Prepare everything with our `accelerator`.
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # Figure out how many steps we should save the Accelerator states
    if hasattr(args.checkpointing_steps, "isdigit"):
        checkpointing_steps = args.checkpointing_steps
        if args.checkpointing_steps.isdigit():
            checkpointing_steps = int(args.checkpointing_steps)
    else:
        checkpointing_steps = None

    # We need to initialize the trackers we use, and also store our configuration.
    # We initialize the trackers only on main process because `accelerator.log`
    # only logs on main process and we don't want empty logs/runs on other processes.
    if args.with_tracking:
        if accelerator.is_main_process:
            experiment_config = vars(args)
            # TensorBoard cannot log Enums, need the raw value
            experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value
            accelerator.init_trackers("glue_no_trainer", experiment_config)

    # Get the metric function
    if args.task_name is not None:
        if args.task_name == 'snli':
            metric = load_metric("glue", 'mnli')
        elif args.task_name in ['boolq','cb']:
            metric = load_metric("super_glue", args.task_name)
        elif 'noisy' in args.task_name:
            task_name = args.task_name.split('-')[0]
            metric = load_metric("glue", task_name)
        elif args.task_name == 'nli-diag':
            metric = load_metric("accuracy")
        else:
            metric = load_metric("glue", args.task_name)
    else:
        metric = load_metric("accuracy")

    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
        else:
            resume_step = int(training_difference.replace("step_", ""))
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)
    
    # ============================ Training Loop ============================
    log_to_file('Validation performance after each training epoch:')

    # ============================------------------------------
    # 9.2 ambiguous first weight
    from torch.nn import CrossEntropyLoss
    # loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1)) 
    my_loss_fct = CrossEntropyLoss(reduction="none")
    def loss_fct_with_sample_weights(logits, labels, weights):
        # weights: list
        losses = my_loss_fct(logits.view(-1, num_labels), labels.view(-1))
        weights = torch.Tensor(weights)
        # weights = accelerator.prepare(weights)
        weights = weights.to(accelerator.device)
        return (losses * weights).mean()
    
    import pickle
    # ============================------------------------------



    for epoch in range(starting_epoch, args.num_train_epochs):

        model.train()
        if args.with_tracking:
            total_loss = 0
        for step, batch in enumerate(train_dataloader):
            # We need to skip steps until we reach the resumed step
            if args.resume_from_checkpoint and epoch == starting_epoch:
                if resume_step is not None and step < resume_step:
                    completed_steps += 1
                    continue
        
            if args.train_with_sample_loss:
                with open('HCT/mnli-roberta-weight-a0.6-k4.weight', 'rb') as handle:
                    idx2weight = pickle.load(handle)
                sample_weights = [idx2weight[int(idx)] for idx in batch['idx']]
                # batch中包含了idx字段，这里需要去除
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                outputs = model(**batch)
                loss = loss_fct_with_sample_weights(outputs.logits, batch['labels'], sample_weights)
            else:
                # batch中包含了idx字段，这里需要去除
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                outputs = model(**batch)
                loss = outputs.loss
            # We keep track of the loss at each epoch
            if args.with_tracking:
                total_loss += loss.detach().float()
            loss = loss / args.gradient_accumulation_steps
            accelerator.backward(loss)
            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
                progress_bar.update(1)
                completed_steps += 1

            if isinstance(checkpointing_steps, int):
                if completed_steps % checkpointing_steps == 0:
                    output_dir = f"step_{completed_steps }"
                    if args.output_dir is not None:
                        output_dir = os.path.join(args.output_dir, output_dir)
                    accelerator.save_state(output_dir)

            if completed_steps >= args.max_train_steps:
                break
        # ------------------ Recording Training Dynamics --------------------
        # 在每一个epoch之后，对train set所有样本再过一遍，记录dynamics
        # 每个epoch单独一个文件
        if args.do_recording:
            if accelerator.is_main_process:
                os.makedirs(f'{args.output_dir}/dy_log/{args.task_name}/{args.model_name_or_path}/training_dynamics/', exist_ok=True)                    
            
            accelerator.wait_for_everyone() # 只在 main process 里面创建文件夹，然后让其他 process 等待 main process 创建完毕
            log_path = f'{args.output_dir}/dy_log/{args.task_name}/{args.model_name_or_path}/training_dynamics/'
            print('-*-*-*- ',log_path, os.path.exists(log_path),accelerator.device)

            logger.info('---------- Recording Training Dynamics (Epoch %s) -----------'%epoch)
            training_dynamics = []
            all_ids = []
            for step, batch in enumerate(tqdm(train_dataloader)):
                # print('- - - - - - - - - -  ',len(batch['idx']), accelerator.device)
                idx_list = batch['idx']#.tolist()
                label_list = batch['labels']#.tolist()
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                logits_list = model(**batch).logits#.tolist() # [[],[],[],...] batch_size个[]
                # 这里的关键：通过 gather 把每个 GPU上的结果合并
                # 由于在使用多卡训练时，不同卡可能存在样本的重复，同一个卡也会对最后一个batch进行补齐，也会样本重复
                # 使用 gather 的话，就可以按照原来的分配方式，逆着组合回去，就不用你自己处理了
                # gather 之后的，在每个卡上，下述变量里包含的数量，都等同于只使用单卡进行训练时的数量
                # 所以下面的for训练执行完之后，training_dynamics里就包含了全部样本，你在写入文件时，记住只在一个 process 中写入
                idx_list, label_list, logits_list = accelerator.gather((idx_list, label_list, logits_list)) 
                # print('idx_list', idx_list.shape, accelerator.device)
                # print('label_list', label_list.shape, accelerator.device)
                
                for idx, label, logits in zip(idx_list.tolist(), label_list.tolist(), logits_list.tolist()):
                    if idx in all_ids: # 由于 data_loader 可能会对最后一个 batch 进行补全，所以这里要去掉重复的样本
                        continue
                    all_ids.append(idx)
                    record = {'guid': idx, 'logits_epoch_%s'%epoch: logits, 'gold': label, 'device':str(accelerator.device)}
                    training_dynamics.append(record)
            
            if accelerator.is_main_process:
                print('---- Num of training_dynamics: ',len(training_dynamics),' Device: ', str(accelerator.device))
                print(len(all_ids),len(list(set(all_ids))),str(accelerator.device))
                assert os.path.exists(log_path),log_path
                writer = open(log_path + f'dynamics_epoch_{epoch}.jsonl', 'w') 
                for record in training_dynamics:
                    writer.write(json.dumps(record) + "\n")
                logger.info(f'Epoch {epoch} Saved to [{log_path}]')
                writer.close()
            accelerator.wait_for_everyone()
        
        # ------------------------------------------------------------------------

        # evaluation (validation set)
        model.eval()
        samples_seen = 0
        for step, batch in enumerate(eval_dataloader):
            batch = {k:v for k,v in batch.items() if k != 'idx'} 
            with torch.no_grad():
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1) if not is_regression else outputs.logits.squeeze()
            predictions, references = accelerator.gather((predictions, batch["labels"]))
            # If we are in a multiprocess environment, the last batch has duplicates
            if accelerator.num_processes > 1:
                if step == len(eval_dataloader) - 1:
                    predictions = predictions[: len(eval_dataloader.dataset) - samples_seen]
                    references = references[: len(eval_dataloader.dataset) - samples_seen]
                else:
                    samples_seen += references.shape[0]
            metric.add_batch(
                predictions=predictions,
                references=references,
            )

        eval_metric = metric.compute()
        logger.info(f"***Evaluation*** epoch {epoch}: {eval_metric}")
        log_to_file(eval_metric)
        

        if args.with_tracking:
            accelerator.log(
                {
                    "accuracy" if args.task_name is not None else "glue": eval_metric,
                    "train_loss": total_loss.item() / len(train_dataloader),
                    "epoch": epoch,
                    "step": completed_steps,
                },
                step=completed_steps,
            )

        if args.checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            accelerator.save_state(output_dir)
    # ============================ End Training Loop ============================


    if args.output_dir is not None and args.resume_from_checkpoint is None: # 提供了path，同时没有指定resume，说明是第一次跑
        accelerator.wait_for_everyone()
        unwrapped_model = accelerator.unwrap_model(model)
        if accelerator.is_main_process:
            unwrapped_model.save_pretrained(
                args.output_dir, save_function=accelerator.save)
            tokenizer.save_pretrained(args.output_dir)

        # accelerator.save_state(args.output_dir)
            # if args.push_to_hub:
            #     repo.push_to_hub(commit_message="End of training", auto_lfs_prune=True)



    # More evaluation
    # e.g. 
    # The mismatch evaluation for MNLI task
    # The test set for tasks with an annotated test set, like SNLI
    if args.task_name == "nli-diag":
        # Final evaluation on mismatched validation set
        eval_dataset = processed_datasets["validation"]
        eval_dataloader = DataLoader(
            eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
        )
        eval_dataloader = accelerator.prepare(eval_dataloader)

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # batch中包含了idx字段，这里需要去除
            batch = {k:v for k,v in batch.items() if k != 'idx'} 
            outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            metric.add_batch(
                predictions=accelerator.gather(predictions),
                references=accelerator.gather(batch["labels"]),
            )

        eval_metric = metric.compute()
        logger.info(f"accuracy: {eval_metric}")
        log_to_file(eval_metric)
    if args.task_name == "mnli":
        log_to_file('\nmis_match evaluation for MNLI:')
        # Final evaluation on mismatched validation set
        eval_dataset = processed_datasets["validation_mismatched"]
        eval_dataloader = DataLoader(
            eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
        )
        eval_dataloader = accelerator.prepare(eval_dataloader)

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # batch中包含了idx字段，这里需要去除
            batch = {k:v for k,v in batch.items() if k != 'idx'} 
            outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            metric.add_batch(
                predictions=accelerator.gather(predictions),
                references=accelerator.gather(batch["labels"]),
            )

        eval_metric = metric.compute()
        logger.info(f"mnli-mm: {eval_metric}")
        log_to_file(eval_metric)

    if args.task_name == "snli":
        log_to_file('\ntest evaluation for SNLI:')
        # Final evaluation on mismatched validation set
        eval_dataset = processed_datasets["test"]
        eval_dataloader = DataLoader(
            eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
        )
        eval_dataloader = accelerator.prepare(eval_dataloader)

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # batch中包含了idx字段，这里需要去除
            batch = {k:v for k,v in batch.items() if k != 'idx'} 
            outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            metric.add_batch(
                predictions=accelerator.gather(predictions),
                references=accelerator.gather(batch["labels"]),
            )

        eval_metric = metric.compute()
        logger.info(f"snli-test: {eval_metric}")
        log_to_file(eval_metric)

# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    # after training, continue train on some data
    if args.continue_train:
        if args.do_lwf:
            # load the orginal trained model
            model_orig = AutoModelForSequenceClassification.from_pretrained(args.output_dir)
            model_orig = accelerator.prepare(model_orig)
            kld_loss_fct = nn.KLDivLoss(reduction="batchmean")

        log_to_file(f'\nContinue Training with subset:')
        # with open(f'dy_log/{args.task_name}/bert-base-cased/three_regions_data_indices.json' ,'r') as f:
        #     d = json.loads(f.read())
        #     selected_indices = d['ambiguous']

        # with open(f'dy_log/{args.task_name}/bert-base-cased/{args.selected_indices_filename}.txt', 'r') as f:
        #     selected_indices = [int(x) for x in f.readlines()]

        # selected_train_dataset = train_dataset.filter(lambda x:x['idx'] in selected_indices)
        selected_train_dataset = train_dataset
        accelerator.print(selected_train_dataset)
        selected_train_dataloader = DataLoader(
            selected_train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
        )
        selected_train_dataloader = accelerator.prepare(selected_train_dataloader)

        # optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

        num_update_steps_per_epoch = math.ceil(len(selected_train_dataloader) / args.gradient_accumulation_steps)
        continue_max_train_steps = args.continue_num_train_epochs * num_update_steps_per_epoch
        continue_lr_scheduler = get_scheduler(
            name=args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=args.num_warmup_steps,
            num_training_steps=continue_max_train_steps,
    )

        for epoch in range(args.continue_num_train_epochs):
            model.train()
            if args.with_tracking:
                total_loss = 0
            for step, batch in enumerate(tqdm(selected_train_dataloader)):
                if args.continue_train_with_sample_loss:
                    sample_weights = [idx2weight[int(idx)] for idx in batch['idx']]
                    # batch中包含了idx字段，这里需要去除
                    batch = {k:v for k,v in batch.items() if k != 'idx'} 
                    outputs = model(**batch)
                    loss = loss_fct_with_sample_weights(outputs.logits, batch['labels'], sample_weights)
                else:
                    # batch中包含了idx字段，这里需要去除
                    batch = {k:v for k,v in batch.items() if k != 'idx'} 
                    outputs = model(**batch)
                    loss = outputs.loss
                # We keep track of the loss at each epoch
                if args.with_tracking:
                    total_loss += loss.detach().float()
                loss = loss / args.gradient_accumulation_steps

                if args.do_lwf:
                    model_orig.train()
                    orig_outputs = model_orig(**batch)
                    orig_logits = orig_outputs.logits
                    new_logits = outputs.logits
                    orig_logits = orig_logits.view(-1, orig_logits.size(-1))
                    new_logits = new_logits.view(-1, new_logits.size(-1))

                    args.temperature = 1
                    args.alpha = 0.5
                    distil_loss = kld_loss_fct(
                                    nn.functional.log_softmax(new_logits / args.temperature, dim=-1),
                                    nn.functional.softmax(orig_logits / args.temperature, dim=-1),
                                 ) * (args.temperature) ** 2
                    loss = args.alpha * distil_loss + loss

                    
                accelerator.backward(loss)
                optimizer.step()
                continue_lr_scheduler.step()
                optimizer.zero_grad()
                    

            # evaluation (validation set)
            model.eval()
            samples_seen = 0
            for step, batch in enumerate(eval_dataloader):
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                with torch.no_grad():
                    outputs = model(**batch)
                predictions = outputs.logits.argmax(dim=-1) if not is_regression else outputs.logits.squeeze()
                predictions, references = accelerator.gather((predictions, batch["labels"]))
                # If we are in a multiprocess environment, the last batch has duplicates
                if accelerator.num_processes > 1:
                    if step == len(eval_dataloader) - 1:
                        predictions = predictions[: len(eval_dataloader.dataset) - samples_seen]
                        references = references[: len(eval_dataloader.dataset) - samples_seen]
                    else:
                        samples_seen += references.shape[0]
                metric.add_batch(
                    predictions=predictions,
                    references=references,
                )

            eval_metric = metric.compute()
            logger.info(f"***Continue Evaluation*** epoch {epoch}: {eval_metric}")
            log_to_file(eval_metric)
    
    if args.continue_train:
        # More evaluation
        # e.g. 
        # The mismatch evaluation for MNLI task
        # The test set for tasks with an annotated test set, like SNLI
        if args.task_name == "mnli":
            log_to_file('\nContinue, mis_match evaluation for MNLI:')
            # Final evaluation on mismatched validation set
            eval_dataset = processed_datasets["validation_mismatched"]
            eval_dataloader = DataLoader(
                eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
            )
            eval_dataloader = accelerator.prepare(eval_dataloader)

            model.eval()
            for step, batch in enumerate(eval_dataloader):
                # batch中包含了idx字段，这里需要去除
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                outputs = model(**batch)
                predictions = outputs.logits.argmax(dim=-1)
                metric.add_batch(
                    predictions=accelerator.gather(predictions),
                    references=accelerator.gather(batch["labels"]),
                )

            eval_metric = metric.compute()
            logger.info(f"mnli-mm: {eval_metric}")
            log_to_file(eval_metric)

        if args.task_name == "snli":
            log_to_file('\nContinue, test evaluation for SNLI:')
            # Final evaluation on mismatched validation set
            eval_dataset = processed_datasets["test"]
            eval_dataloader = DataLoader(
                eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
            )
            eval_dataloader = accelerator.prepare(eval_dataloader)

            model.eval()
            for step, batch in enumerate(eval_dataloader):
                # batch中包含了idx字段，这里需要去除
                batch = {k:v for k,v in batch.items() if k != 'idx'} 
                outputs = model(**batch)
                predictions = outputs.logits.argmax(dim=-1)
                metric.add_batch(
                    predictions=accelerator.gather(predictions),
                    references=accelerator.gather(batch["labels"]),
                )

            eval_metric = metric.compute()
            logger.info(f"snli-test: {eval_metric}")
            log_to_file(eval_metric)
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!    

    # if args.output_dir is not None:
    #     with open(os.path.join(args.output_dir, "all_results.json"), "w") as f:
    #         json.dump({"eval_accuracy": eval_metric["accuracy"]}, f)


if __name__ == "__main__":
    main()