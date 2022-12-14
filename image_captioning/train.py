import argparse
import transformers
import torch
import datasets
import evaluate
import PIL
import os
import pathlib

import numpy as np
import pandas as pd

from tqdm.notebook import tqdm
from .utils import (
    seed_everything,
    save_args
)


def main(args: argparse.Namespace):
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    print('train: args=%s' % args.__dict__)
    save_args(args, args.output_dir / 'train_args.json')

    model = transformers.VisionEncoderDecoderModel.from_encoder_decoder_pretrained(
        args.encoder_model_name_or_path, args.decoder_model_name_or_path
    )
    feature_extractor = transformers.AutoFeatureExtractor.from_pretrained(args.encoder_model_name_or_path)
    tokenizer = transformers.AutoTokenizer.from_pretrained(args.decoder_model_name_or_path)
    if tokenizer.cls_token_id is not None:
        model.config.decoder_start_token_id = tokenizer.cls_token_id
    else:
        model.config.decoder_start_token_id = tokenizer.bos_token_id
    if tokenizer.pad_token_id is not None:
        model.config.pad_token_id = tokenizer.pad_token_id
    else:
        # https://github.com/huggingface/transformers/issues/7135#issuecomment-693524590
        model.config.pad_token_id = tokenizer.eos_token_id

    train_dataset = datasets.load_dataset(
        "kumapo/stair_captions_dataset_script", "2014", 
        data_dir=str(args.train_data_dir), split="train", streaming=True
    )
    eval_dataset = datasets.load_dataset(
        "kumapo/stair_captions_dataset_script", "2014",
        data_dir=str(args.valid_data_dir), split="validation", streaming=True
    )
    # https://github.com/huggingface/datasets/issues/4675
    def preprocess_function(examples):
        do_padding = False if tokenizer.pad_token_id is None else True
        # prepare image (i.e. resize + normalize)
        pixel_values = feature_extractor(
            [PIL.Image.open(path).convert("RGB") for path in examples['image_path']],
            return_tensors="np"
        ).pixel_values
        # add labels (input_ids) by encoding the text
        encoded = tokenizer(
            [label for label in examples['caption']], 
            padding="max_length" if do_padding else "do_not_pad",
            max_length=args.max_sequence_length,
            truncation=True,
            return_tensors="np",
            return_length=True
        )
        del examples
        if do_padding:
            # important: make sure that PAD tokens are ignored by the loss function
            encoded.input_ids[encoded.input_ids == tokenizer.pad_token_id] = -100
        else:
            encoded.input_ids = [
                input_ids + ([-100] * (args.max_sequence_length - len(input_ids)))
                for input_ids in encoded.input_ids
            ]
        return {
            "pixel_values": pixel_values.squeeze(),
            "labels": encoded.input_ids,
            # "length": encoded.length
        }

    train_dataset = train_dataset.map(
        preprocess_function,
        batched=True,
        remove_columns=["image_path", "caption", 'image_id', 'width', 'file_name', 'coco_url', 'caption_id', 'height']
    )
    eval_dataset = eval_dataset.map(
        preprocess_function,
        batched=True,
        # batch_size=args.valid_batch_size,
        # writer_batch_size=args.valid_batch_size,
        remove_columns=["image_path", "caption", 'image_id', 'width', 'file_name', 'coco_url', 'caption_id', 'height']
    )
    # train_dataloader = torch.utils.data.DataLoader(
    #     # https://github.com/huggingface/datasets/discussions/2577
    #     train_dataset.shuffle(seed=args.random_seed, buffer_size=1000).take(args.num_train_data).with_format("torch"),
    #     # take and skip prevent future calls to shuffle because they lock in the order of the shards. 
    #     # You should shuffle your dataset before splitting it.
    #     batch_size=args.train_batch_size,
    #     num_workers=args.num_workers # must be 1 otherwise a thread crashs
    # )
    # eval_dataloader = torch.utils.data.DataLoader(
    #     eval_dataset.take(args.num_valid_data).with_format("torch"),
    #     batch_size=args.valid_batch_size,
    #     num_workers=args.num_workers # above
    # )
    train_dataset = train_dataset.shuffle(seed=args.random_seed, buffer_size=1000).with_format("torch")
    if 0 < args.num_valid_data:
        eval_dataset = datasets.Dataset.from_dict(
            eval_dataset._head(args.num_valid_data),
            features=datasets.Features({
                "pixel_values": datasets.Array3D(shape=(3, 224, 224), dtype='float32'),
                "labels": datasets.Sequence(feature=datasets.Value(dtype='int32'), length=args.max_sequence_length)
            })
        ).with_format("torch")
    else:
        eval_dataset = eval_dataset.with_format("torch")

    max_steps = (args.num_train_epochs * args.num_train_data) // args.train_batch_size
    print("max_steps: ", max_steps)
    # https://github.com/NielsRogge/Transformers-Tutorials/blob/master/TrOCR/Fine_tune_TrOCR_on_IAM_Handwriting_Database_using_Seq2SeqTrainer.ipynb
    training_args = transformers.Seq2SeqTrainingArguments(
        # https://github.com/huggingface/transformers/issues/12499
        # num_train_epochs=args.num_train_epochs,
        max_steps=max_steps,
        predict_with_generate=True,
        evaluation_strategy="steps",
        per_device_train_batch_size=args.train_batch_size,
        per_device_eval_batch_size=args.valid_batch_size,
        fp16=not args.no_fp16 if not args.debug else False,
        output_dir=args.output_dir,
        eval_steps=args.eval_steps,
        logging_steps=args.eval_steps,
        save_steps=args.eval_steps,
        save_total_limit=1,
        load_best_model_at_end=True,
        metric_for_best_model='eval_loss',
        dataloader_num_workers=args.num_workers if not args.debug else 0,
        report_to="tensorboard",
        seed=args.random_seed
    )

    bleu = evaluate.load("sacrebleu")
    rouge = evaluate.load("rouge")
    meteor = evaluate.load("meteor")
    def compute_metrics(pred):
        labels_ids = pred.label_ids
        pred_ids = pred.predictions
        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        if tokenizer.pad_token_id is not None:
            labels_ids[labels_ids == -100] = tokenizer.pad_token_id
        else:
            # special tokens are skipped
            labels_ids[labels_ids == -100] = tokenizer.eos_token_id
        label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
        metrics = {}
        metrics.update(bleu.compute(
            predictions=pred_str, references=label_str,
            smooth_method="floor", smooth_value=0.1, tokenize='ja-mecab'
        ))
        metrics.update(rouge.compute(
            predictions=pred_str, references=label_str,
            tokenizer=lambda x: tokenizer.tokenize(x)
        ))
        metrics.update(meteor.compute(predictions=pred_str, references=label_str))
        return metrics

    # instantiate trainer
    trainer = transformers.Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        compute_metrics=compute_metrics,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=transformers.default_data_collator,
    )
    trainer.train()

    # evaluate
    training_args = transformers.Seq2SeqTrainingArguments(
        predict_with_generate=True,
        per_device_eval_batch_size=args.valid_batch_size,
        fp16=not args.no_fp16 if not args.debug else False,
        output_dir=args.output_dir,
        report_to="tensorboard",
        seed=args.random_seed
    )
    trainer = transformers.Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        compute_metrics=compute_metrics,
        eval_dataset=eval_dataset,
        data_collator=transformers.default_data_collator,
    )
    # copied from evaluate.py
    gen_kwargs = dict(
        do_sample=False,
        max_new_tokens=args.max_new_tokens,
        # max_length=args.max_new_tokens + 1, # workaround
        num_beams=5,
        no_repeat_ngram_size=2,
        num_return_sequences=1,
        early_stopping=True
    )
    # gen_kwargs = dict(
    #     do_sample=True, 
    #     max_length=args.max_new_tokens + 1, # workaround
    #     top_k=50, 
    #     top_p=0.9, 
    #     num_return_sequences=1
    # )
    metrics = trainer.evaluate(eval_dataset, **gen_kwargs)
    print("Validation metrics:", metrics)

    # save finally
    model.save_pretrained(args.output_dir)
    feature_extractor.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--train_data_dir", default='../input/coco-2014-train/', type=pathlib.Path
    )
    parser.add_argument(
        "--valid_data_dir", default='../input/coco-2014-val/', type=pathlib.Path
    )
    parser.add_argument(
        "--output_dir", default=pathlib.Path('output'), type=pathlib.Path, help=""
    )
    parser.add_argument(
        "--encoder_model_name_or_path", default="microsoft/swin-base-patch4-window7-224-in22k", type=str, help=""
    )
    parser.add_argument(
        "--decoder_model_name_or_path", default="cl-tohoku/bert-base-japanese-v2", type=str, help=""
    )
    parser.add_argument(
        "--max_sequence_length", default=64, type=int, help=""
    )
    parser.add_argument(
        "--max_new_tokens", default=16, type=int, help="which ignores the number of tokens in the prompt."
    )
    parser.add_argument(
        "--num_train_epochs", default=1, type=int, help=""
    )
    parser.add_argument(
        "--learning_rate", default=5e-5, type=float, help=""
    )
    parser.add_argument(
        "--train_batch_size", default=32, type=int, help=""
    )
    parser.add_argument(
        "--valid_batch_size", default=32, type=int, help=""
    )
    parser.add_argument(
        "--num_workers", default=2, type=int, help=""
    )
    parser.add_argument(
        "--no_fp16", action="store_true", help=""
    )
    parser.add_argument(
        "--random_seed", default=42, type=int, help="Random seed for determinism."
    )
    parser.add_argument(
        "--num_train_data", default=10000, type=int, help="number of items to train on dataset."
    )
    parser.add_argument(
        "--num_valid_data", default=1000, type=int, help="number of items to evaluate on dataset."
    )
    parser.add_argument(
        "--eval_steps", default=600, type=int, help="steps = num_data // batch_size"
    )
    parser.add_argument(
        "--debug", action="store_true",
    )
    args = parser.parse_args()
    seed_everything(args.random_seed)
    main(args)
