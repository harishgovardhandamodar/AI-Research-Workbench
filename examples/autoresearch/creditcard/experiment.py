"""Autonomous-research target for the Kaggle credit-card fraud demo.

Edit this file to improve the goal metric (**roc_auc**, higher is better).
The autoresearch harness runs it under a fixed time budget and reads the final
line:

    METRIC roc_auc=<value>

Dataset: <project>/data/creditcard.csv (the classic Kaggle creditcardfraud
dataset — 284,807 transactions, extremely imbalanced: 0.17% fraud). Accuracy is
misleading here, so we optimise ROC-AUC.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

DATA = Path(__file__).resolve().parent.parent / "data" / "creditcard.csv"


def load():
    df = pd.read_csv(DATA)
    y = df["Class"].values
    X = df.drop(columns=["Class"]).values
    return X, y


def main():
    X, y = load()
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2,
                                          random_state=7, stratify=y)
    sc = StandardScaler()
    Xtr = sc.fit_transform(Xtr)
    Xte = sc.transform(Xte)
    model = LogisticRegression(max_iter=500)
    model.fit(Xtr, ytr)
    auc = roc_auc_score(yte, model.predict_proba(Xte)[:, 1])
    print(f"METRIC roc_auc={auc:.4f}")


if __name__ == "__main__":
    main()
