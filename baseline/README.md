# TF-IDF Baseline Evaluation

## Overview

This baseline evaluates a text-based classification approach for distinguishing between `allow` and `deny` decisions in the MayI project.

The baseline uses **TF-IDF vectorization** with **Logistic Regression**. Two input representations are evaluated independently:

- **Input A (`input_a`):** Command-only input.
- **Input B (`input_b`):** Contextual input.

The objective is to establish a reproducible reference point for future model development and compare performance across standard and out-of-distribution (OOD) datasets.

## Methodology

### Text Representation

The TF-IDF vectorizer uses the following configuration:

- Lowercase normalization.
- Unicode accent stripping.
- Unigrams and bigrams.
- Minimum document frequency of 2.
- Maximum of 100,000 features.
- Sublinear term-frequency scaling.

### Classifier

Logistic Regression is used as the baseline classifier with:

- `max_iter=200`
- `solver="liblinear"`
- `random_state=42`

The model is trained separately for each input representation. Only the selected text field is used as model input.

## Evaluation Datasets

The evaluation covers three dataset categories:

| Split | Dataset | Purpose |
|---|---|---|
| Validation | `val.csv.gz` | Model development and validation |
| Test | `test.csv.gz` | Evaluation on the held-out test split |
| OOD | `ood_approve_or_deny.csv.gz` | Generalization to external examples |
| OOD | `ood_saroku.csv.gz` | Generalization to a different data distribution |

The label mapping is:

- `0`: Allow
- `1`: Deny

## Metrics

The following metrics are used to assess classification performance:

- **Accuracy:** Fraction of correctly classified examples.
- **Deny precision:** Fraction of predicted deny decisions that are actually deny.
- **Deny recall:** Fraction of actual deny decisions correctly identified.
- **Deny F1:** Harmonic mean of deny precision and deny recall.
- **Macro-F1:** Average F1 score across both classes.
- **False allow rate:** Fraction of actual deny examples incorrectly classified as allow.
- **False deny rate:** Fraction of actual allow examples incorrectly classified as deny.
- **Confusion matrix:** Counts of correct and incorrect predictions for each class.

For safety-oriented interpretation, false allows are particularly important because they represent deny-labeled examples classified as allow.

## Results Summary

The current baseline results show the following general pattern:

- `input_b` performs better than `input_a` on the reported validation and test accuracy results.
- Performance decreases on OOD data, indicating a generalization gap between standard evaluation data and external distributions.
- OOD performance varies by dataset, so results should be reported separately for each OOD dataset rather than combined into a single score.

Exact metric values should be taken from the generated evaluation result files to ensure reproducibility.

## Limitations

- TF-IDF does not represent semantic relationships as deeply as contextual neural language models.
- Performance on validation and test splits does not guarantee robust behavior on unseen distributions.
- Aggregate metrics can conceal class-specific failure modes.
- False allow and false deny rates should be considered alongside accuracy.
-
