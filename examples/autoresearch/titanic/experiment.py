"""Autonomous-research target for the Titanic survival demo.

Edit this file to improve the goal metric (accuracy, higher is better). The
autoresearch harness runs it under a fixed time budget and reads the final line:

    METRIC accuracy=<value>

Dataset: <project>/data/titanic_train.csv (the classic Kaggle Titanic dataset).
The path is resolved relative to this file, so the script works regardless of cwd.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score

DATA = Path(__file__).resolve().parent.parent / "data" / "titanic_train.csv"


def load():
    df = pd.read_csv(DATA)
    df = df.drop(columns=["PassengerId", "Name", "Ticket", "Cabin"])
    df["Sex"] = (df["Sex"] == "male").astype(int)
    df["Embarked"] = df["Embarked"].fillna("S").map({"S": 0, "C": 1, "Q": 2}).fillna(0)
    for c in df.columns:
        if df[c].dtype == "float64":
            df[c] = df[c].fillna(df[c].median())
    y = df["Survived"].values
    X = df.drop(columns=["Survived"]).values
    return X, y


def main():
    X, y = load()
    model = LogisticRegression(max_iter=500)
    acc = cross_val_score(model, X, y, cv=5, scoring="accuracy").mean()
    print(f"METRIC accuracy={acc:.4f}")


if __name__ == "__main__":
    main()
