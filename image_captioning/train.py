import argparse
import transformers
import torch
import datasets
import evaluate
import PIL
import os
import pathlib

import numpy as np

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

    feature_extractor = transformers.DeiTFeatureExtractor.from_pretrained(args.encoder_model_name_or_path)
    tokenizer = transformers.ElectraTokenizer.from_pretrained(args.decoder_model_name_or_path)
    model = transformers.VisionEncoderDecoderModel.from_encoder_decoder_pretrained(
        args.encoder_model_name_or_path, args.decoder_model_name_or_path
    )
    model.config.decoder_start_token_id = tokenizer.cls_token_id
    model.config.pad_token_id = tokenizer.pad_token_id

    train_dataset = datasets.load_dataset(
        "kumapo/coco_dataset_script", "2017", 
        data_dir=str(args.train_data_dir), split="train", streaming=True
    )
    eval_dataset = datasets.load_dataset(
        "kumapo/coco_dataset_script", "2017",
        data_dir=str(args.valid_data_dir), split="validation", streaming=True
    )
    # https://github.com/huggingface/datasets/issues/4675
    def preprocess_function(examples):
        # prepare image (i.e. resize + normalize)
        pixel_values = feature_extractor(
            [PIL.Image.open(path).convert("RGB") for path in examples['image_path']],
            return_tensors="np"
        ).pixel_values
        # add labels (input_ids) by encoding the text
        encoded = tokenizer(
            [label for label in examples['caption']], 
            padding="max_length",
            max_length=args.max_sequence_length,
            return_tensors="np",
            return_length=True
        )
        del examples
        # important: make sure that PAD tokens are ignored by the loss function
        encoded.input_ids[encoded.input_ids == tokenizer.pad_token_id] = -100
        return {
            "pixel_values": pixel_values.squeeze(),
            "labels": encoded.input_ids,
            "length": encoded.length
        }

    train_dataset = train_dataset.map(
        preprocess_function,
        batched=True,
        remove_columns=["image_path","caption"]
    )
    eval_dataset = eval_dataset.map(
        preprocess_function,
        batched=True,
        remove_columns=["image_path","caption"]
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
    eval_dataset = eval_dataset.take(args.num_valid_data).with_format("torch")

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
        fp16=False if args.debug else not args.no_fp16, 
        output_dir=args.output_dir,
        logging_steps=300,
        save_steps=300,
        eval_steps=300,
        save_total_limit=1,
        load_best_model_at_end=True,
        metric_for_best_model='eval_loss',
        # dataloader_num_workers=args.num_workers,
        report_to="tensorboard",
        seed=args.random_seed
    )

    bleu = evaluate.load("bleu")
    meteor = evaluate.load("meteor")
    def compute_metrics(pred):
        labels_ids = pred.label_ids
        pred_ids = pred.predictions
        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        labels_ids[labels_ids == -100] = tokenizer.pad_token_id
        label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
        metrics = {}
        try:
            metrics.update(bleu.compute(predictions=pred_str, references=label_str))
        except ZeroDivisionError as e:
            metrics.update(dict(bleu="nan"))
        try:
            metrics.update(meteor.compute(predictions=pred_str, references=label_str))
        except ZeroDivisionError as e:
            metrics.update(dict(meteor="nan"))
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

    # https://github.com/huggingface/transformers/blob/v4.21.1/src/transformers/generation_utils.py#L845
    gen_kwargs = dict(
        do_sample=True, 
        max_length=args.max_sequence_length, 
        top_k=50, 
        top_p=0.95, 
        num_return_sequences=1
    )
    # evaluate
    metrics = trainer.evaluate(eval_dataset, **gen_kwargs)
    print("Validation metrics:", metrics)

    # prediction
    pred = trainer.predict(eval_dataset[:3], **gen_kwargs)
    labels_ids = pred.label_ids
    pred_ids = pred.predictions
    pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
    pred_str = [pred for pred in pred_str]
    labels_ids[labels_ids == -100] = tokenizer.pad_token_id
    label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
    print("Validation predictions:", pred_str)
    print("Validation labels:", label_str)

    # save finally
    model.save_pretrained(args.output_dir)
    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--train_data_dir", default='../input/coco-2017-train/', type=pathlib.Path
    )
    parser.add_argument(
        "--valid_data_dir", default='../input/coco-2017-val/', type=pathlib.Path
    )
    parser.add_argument(
        "--output_dir", default=pathlib.Path('output'), type=pathlib.Path, help=""
    )
    parser.add_argument(
        "--encoder_model_name_or_path", default="microsoft/swin-base-patch4-window7-224-in22k", type=str, help=""
    )
    parser.add_argument(
        "--decoder_model_name_or_path", default="bert-base-uncased", type=str, help=""
    )
    parser.add_argument(
        "--max_sequence_length", default=64, type=int, help=""
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
    # parser.add_argument(
    #     "--num_workers", default=1, type=int, help=""
    # )
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
        "--num_valid_data", default=2000, type=int, help="number of items to evaluate on dataset."
    )
    parser.add_argument(
        "--debug", action="store_true",
    )
    args = parser.parse_args()
    seed_everything(args.random_seed)
    main(args)