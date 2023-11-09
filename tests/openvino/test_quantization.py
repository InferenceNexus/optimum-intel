#  Copyright 2021 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# ruff: noqa

import tempfile
import unittest
from functools import partial

import evaluate
import numpy as np
from datasets import load_dataset
from parameterized import parameterized
from transformers import (
    AutoModelForQuestionAnswering,
    AutoModelForSequenceClassification,
    AutoModelForCausalLM,
    AutoModelForTokenClassification,
    AutoTokenizer,
    TrainingArguments,
    default_data_collator,
)

from optimum.intel import (
    OVConfig,
    OVModelForAudioClassification,
    OVModelForCausalLM,
    OVModelForFeatureExtraction,
    OVModelForImageClassification,
    OVModelForMaskedLM,
    OVModelForQuestionAnswering,
    OVModelForSeq2SeqLM,
    OVModelForSequenceClassification,
    OVModelForTokenClassification,
    OVStableDiffusionPipeline,
    OVStableDiffusionXLPipeline,
    OVQuantizer,
    OVTrainer,
)


from optimum.intel.openvino.configuration import INT8_WEIGHT_COMPRESSION_CONFIG
from utils_tests import MODEL_NAMES, get_num_quantized_nodes, _ARCHITECTURES_TO_EXPECTED_INT8

_TASK_TO_DATASET = {
    "text-generation": ("wikitext", "wikitext-2-raw-v1", "text"),
    "text-classification": ("glue", "sst2", "sentence"),
    "text2text-generation": ("wikitext", "wikitext-2-raw-v1", "text"),
}


class OVQuantizerTest(unittest.TestCase):
    # TODO : add models
    SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS = (
        (OVModelForSequenceClassification, "hf-internal-testing/tiny-random-bert", 32, 35),
        (OVModelForCausalLM, "hf-internal-testing/tiny-random-gpt2", 41, 23),
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS)
    def test_automodel_static_quantization(self, model_cls, model_name, expected_fake_quantize, expected_int8):
        task = model_cls.export_feature
        dataset_name, dataset_config_name, column_name = _TASK_TO_DATASET[task]
        file_name = "openvino_quantized_model.xml"

        def preprocess_function(examples, tokenizer):
            return tokenizer(examples[column_name], padding="max_length", max_length=128, truncation=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = model_cls.auto_model_class.from_pretrained(model_name)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            quantizer = OVQuantizer.from_pretrained(transformers_model, task=task)

            calibration_dataset = quantizer.get_calibration_dataset(
                dataset_name,
                dataset_config_name=dataset_config_name,
                preprocess_function=partial(preprocess_function, tokenizer=tokenizer),
                num_samples=10,
                dataset_split="train",
            )
            quantizer.quantize(save_directory=tmp_dir, calibration_dataset=calibration_dataset, file_name=file_name)
            model = model_cls.from_pretrained(tmp_dir, file_name=file_name)

            # TODO: uncomment once move to a newer version of NNCF which has some fixes (addmm, baddmm)
            # num_fake_quantize, num_int8 = get_num_quantized_nodes(model)
            # self.assertEqual(expected_fake_quantize, num_fake_quantize)
            # self.assertEqual(expected_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model(**tokens)
            self.assertTrue("logits" in outputs)

            # Verify that that the configuration is correctly saved and loaded
            expected_config = OVConfig()
            loaded_config = OVConfig.from_pretrained(tmp_dir)
            self.assertEqual(expected_config.to_dict()["compression"], loaded_config.to_dict()["compression"])

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS)
    def test_ovmodel_static_quantization(self, model_cls, model_name, expected_fake_quantize, expected_int8):
        task = model_cls.export_feature
        dataset_name, dataset_config_name, column_name = _TASK_TO_DATASET[task]
        if "gpt2" in model_name:
            expected_int8 -= 1

        def preprocess_function(examples, tokenizer):
            return tokenizer(examples[column_name], padding="max_length", max_length=128, truncation=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = model_cls.from_pretrained(model_name, export=True)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            quantizer = OVQuantizer.from_pretrained(transformers_model, task=task)

            calibration_dataset = quantizer.get_calibration_dataset(
                dataset_name,
                dataset_config_name=dataset_config_name,
                preprocess_function=partial(preprocess_function, tokenizer=tokenizer),
                num_samples=10,
                dataset_split="train",
            )
            quantizer.quantize(save_directory=tmp_dir, calibration_dataset=calibration_dataset)

            model = model_cls.from_pretrained(tmp_dir)

            num_fake_quantize, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(expected_fake_quantize, num_fake_quantize)
            self.assertEqual(expected_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model(**tokens)
            self.assertTrue("logits" in outputs)


class OVQuantizerSeq2SeqTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS = (
        ("hf-internal-testing/tiny-random-t5", 30, 32, 104, 84),
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS)
    def test_ovmodel_hybrid_quantization(
        self,
        model_name,
        expected_encoder_fq,
        expected_encoder_int8,
        expected_decoder_int8,
        expected_decoder_with_past_int8,
    ):
        task = OVModelForSeq2SeqLM.export_feature
        dataset_name, dataset_config_name, column_name = _TASK_TO_DATASET[task]

        def preprocess_function(examples, tokenizer):
            return tokenizer(examples[column_name], padding="max_length", max_length=128, truncation=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = "t5_test"
            transformers_model = OVModelForSeq2SeqLM.from_pretrained(model_name, export=True, use_cache=True)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            quantizer = OVQuantizer.from_pretrained(transformers_model, task=task)
            calibration_dataset = quantizer.get_calibration_dataset(
                dataset_name,
                dataset_config_name=dataset_config_name,
                preprocess_function=partial(preprocess_function, tokenizer=tokenizer),
                num_samples=10,
                dataset_split="train",
            )
            quantizer.quantize(save_directory=tmp_dir, calibration_dataset=calibration_dataset)
            model = OVModelForSeq2SeqLM.from_pretrained(tmp_dir, use_cache=True)

            num_fake_quantize, num_int8 = get_num_quantized_nodes(model.encoder.model)
            self.assertEqual(expected_encoder_fq, num_fake_quantize)
            self.assertEqual(expected_encoder_int8, num_int8)

            _, num_int8 = get_num_quantized_nodes(model.decoder.model)
            self.assertEqual(expected_decoder_int8, num_int8)

            if model.use_cache:
                _, num_int8 = get_num_quantized_nodes(model.decoder_with_past.model)
            self.assertEqual(expected_decoder_with_past_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model.generate(**tokens)


class OVWeightCompressionTest(unittest.TestCase):
    # TODO : add models
    SUPPORTED_ARCHITECTURES_WITH_EXPECTED_COMPRESSED_MATMULS = (
        (OVModelForSequenceClassification, "hf-internal-testing/tiny-random-bert", 70, 35),
        (OVModelForCausalLM, "hf-internal-testing/tiny-random-BartForCausalLM", 27, 14),
    )

    SUPPORTED_ARCHITECTURES_WITH_AUTO_COMPRESSION = (
        (OVModelForCausalLM, "gpt2"),
        (OVModelForMaskedLM, "bert"),
        (OVModelForTokenClassification, "roberta"),
        (OVModelForImageClassification, "vit"),
        (OVModelForSeq2SeqLM, "t5"),
        (OVModelForSequenceClassification, "albert"),
        (OVModelForQuestionAnswering, "distilbert"),
        (OVModelForAudioClassification, "wav2vec2"),
        (OVModelForFeatureExtraction, "blenderbot"),
        (OVStableDiffusionPipeline, "stable-diffusion"),
        (OVStableDiffusionXLPipeline, "stable-diffusion-xl"),
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_COMPRESSED_MATMULS)
    def test_automodel_weight_compression(self, model_cls, model_name, expected_pt_int8, expected_ov_int8):
        task = model_cls.export_feature

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = model_cls.auto_model_class.from_pretrained(model_name)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            quantizer = OVQuantizer.from_pretrained(transformers_model, task=task)
            quantizer.quantize(save_directory=tmp_dir, weights_only=True)
            model = model_cls.from_pretrained(tmp_dir)

            _, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(expected_pt_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model(**tokens)
            self.assertTrue("logits" in outputs)

            # Verify that that the configuration is correctly saved and loaded
            loaded_config = OVConfig.from_pretrained(tmp_dir)
            self.assertIsNotNone(loaded_config)

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_COMPRESSED_MATMULS)
    def test_ovmodel_weight_compression(self, model_cls, model_name, expected_pt_int8, expected_ov_int8):
        task = model_cls.export_feature

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = model_cls.from_pretrained(model_name, export=True)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            quantizer = OVQuantizer.from_pretrained(transformers_model, task=task)
            quantizer.quantize(save_directory=tmp_dir, weights_only=True)
            model = model_cls.from_pretrained(tmp_dir)

            _, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(expected_ov_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model(**tokens)
            self.assertTrue("logits" in outputs)

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_AUTO_COMPRESSION)
    def test_ovmodel_load_with_compressed_weights(self, model_cls, model_type):
        model = model_cls.from_pretrained(MODEL_NAMES[model_type], export=True, load_in_8bit=True)

        if model.export_feature.startswith("text2text-generation"):
            models = [model.encoder, model.decoder, model.decoder_with_past]
        elif model.export_feature.startswith("stable-diffusion"):
            models = [model.unet, model.vae_encoder, model.vae_decoder]
            models.append(model.text_encoder if model.export_feature == "stable-diffusion" else model.text_encoder_2)
        else:
            models = [model]

        expected_ov_int8 = _ARCHITECTURES_TO_EXPECTED_INT8[model_type]
        for i, model in enumerate(models):
            _, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(expected_ov_int8[i], num_int8)

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_AUTO_COMPRESSION)
    def test_ovmodel_load_with_uncompressed_weights(self, model_cls, model_type):
        model = model_cls.from_pretrained(MODEL_NAMES[model_type], export=True, load_in_8bit=False)

        if model.export_feature.startswith("text2text-generation"):
            models = [model.encoder, model.decoder, model.decoder_with_past]
        elif model.export_feature.startswith("stable-diffusion"):
            models = [model.unet, model.vae_encoder, model.vae_decoder]
            models.append(model.text_encoder if model.export_feature == "stable-diffusion" else model.text_encoder_2)
        else:
            models = [model]

        for i, model in enumerate(models):
            _, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(0, num_int8)


class OVQuantizerQATest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (("hf-internal-testing/tiny-random-BertForQuestionAnswering",),)

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_automodel_static_quantization(self, model_name):
        def preprocess_function(examples, tokenizer):
            return tokenizer(
                examples["question"], examples["context"], padding="max_length", max_length=64, truncation=True
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = AutoModelForQuestionAnswering.from_pretrained(model_name)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            quantizer = OVQuantizer.from_pretrained(transformers_model)
            calibration_dataset = quantizer.get_calibration_dataset(
                "squadshifts",
                dataset_config_name="new_wiki",
                preprocess_function=partial(preprocess_function, tokenizer=tokenizer),
                num_samples=10,
                dataset_split="test",
            )
            quantizer.quantize(save_directory=tmp_dir, calibration_dataset=calibration_dataset)

            # Test that inference on quantized model works
            model = OVModelForQuestionAnswering.from_pretrained(tmp_dir)
            tokens = tokenizer.encode_plus(
                "This is a sample question", "This is a sample context", add_special_tokens=True, return_tensors="pt"
            )
            model(**tokens, return_dict=True)

            # Test loading model a second time to catch issues with caching
            try:
                model = OVModelForQuestionAnswering.from_pretrained(tmp_dir)
            except RuntimeError:
                self.fail("Loading BERT QA model a second time failed")

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_ovmodel_static_quantization(self, model_name):
        def preprocess_function(examples, tokenizer):
            return tokenizer(
                examples["question"], examples["context"], padding="max_length", max_length=64, truncation=True
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            transformers_model = OVModelForQuestionAnswering.from_pretrained(model_name, export=True)
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            quantizer = OVQuantizer.from_pretrained(transformers_model)
            calibration_dataset = quantizer.get_calibration_dataset(
                "squadshifts",
                dataset_config_name="new_wiki",
                preprocess_function=partial(preprocess_function, tokenizer=tokenizer),
                num_samples=10,
                dataset_split="test",
            )
            quantizer.quantize(save_directory=tmp_dir, calibration_dataset=calibration_dataset)

            # Test that inference on quantized model works
            model = OVModelForQuestionAnswering.from_pretrained(tmp_dir)
            tokens = tokenizer.encode_plus(
                "This is a sample question", "This is a sample context", add_special_tokens=True, return_tensors="pt"
            )
            model(**tokens, return_dict=True)

            # Test loading model a second time to catch issues with caching
            try:
                model = OVModelForQuestionAnswering.from_pretrained(tmp_dir)
            except RuntimeError:
                self.fail("Loading BERT QA model a second time failed")


class OVTrainerTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS = (("distilbert-base-uncased", 50, 38),)

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_EXPECTED_QUANTIZED_MATMULS)
    def test_aware_training_quantization(self, model_name, expected_fake_quantize, expected_int8):
        model = AutoModelForSequenceClassification.from_pretrained(model_name)
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        ov_config = OVConfig()
        dataset = load_dataset("glue", "sst2")
        dataset = dataset.map(
            lambda examples: tokenizer(examples["sentence"], padding="max_length", max_length=128), batched=True
        )
        train_dataset = dataset["train"].select(range(16))
        eval_dataset = dataset["validation"].select(range(16))
        metric = evaluate.load("glue", "sst2")

        def compute_metrics(p):
            return metric.compute(predictions=np.argmax(p.predictions, axis=1), references=p.label_ids)

        with tempfile.TemporaryDirectory() as tmp_dir:
            trainer = OVTrainer(
                model=model,
                ov_config=ov_config,
                task="sequence-classification",
                args=TrainingArguments(tmp_dir, num_train_epochs=1.0, do_train=True, do_eval=True),
                train_dataset=train_dataset,
                eval_dataset=eval_dataset,
                compute_metrics=compute_metrics,
                tokenizer=tokenizer,
                data_collator=default_data_collator,
            )
            self.assertEqual(trainer.task, "text-classification")
            trainer.train()
            trainer.evaluate()
            trainer.save_model()

            model = OVModelForSequenceClassification.from_pretrained(tmp_dir)
            num_fake_quantize, num_int8 = get_num_quantized_nodes(model.model)
            self.assertEqual(expected_fake_quantize, num_fake_quantize)
            self.assertEqual(expected_int8, num_int8)

            tokens = tokenizer("This is a sample input", return_tensors="pt")
            outputs = model(**tokens)
            self.assertTrue("logits" in outputs)
