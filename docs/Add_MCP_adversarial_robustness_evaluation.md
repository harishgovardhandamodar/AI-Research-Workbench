# Adding the Adversarial Robustness MCP Server via the Agent GUI

The workbench ships the **adversarial robustness evaluation** MCP server as a file
(`mcp_servers/robustness_tools.py`) — but **not enabled by default**. Enable it in
one minute from the **Agent** dashboard. No code edits, no container rebuilds.

What you get (namespaced `robustness__<tool>`):

| Tool | Purpose |
|------|---------|
| `robustness__evaluate_sklearn_robustness` | Full ART evasion evaluation (FGSM / PGD) of an sklearn classifier |
| `robustness__robustness_metrics_from_predictions` | Clean / robust accuracy + ASR from pre-computed predictions (no extra libraries) |
| `robustness__adversarial_robustness_checklist` | Threat-model + evaluation checklist |
| `robustness__simple_fgsm_perturbation` | Lightweight L∞ perturbation demo on a numeric vector |

---

## Step-by-step (Agent dashboard)

1. Open the workbench and click the **Agent** tab in the top bar
   (Chat | Experiments | Agent).

2. In the **🔌 MCP servers** card, open the **"+ Add MCP server"** form.

3. Fill in the fields:

   | Field | Value |
   |-------|-------|
   | **name** | `robustness` |
   | **transport** | `stdio` |
   | **command** | `{python}` *(placeholder — resolves to the container's Python)* |
   | **args** | `mcp_servers/robustness_tools.py` |
   | **trusted** | ✅ check it *(read-only tools run freely; unchecking keeps the human-in-the-loop approval prompts)* |

4. Click **Add**. The server is saved to the config and the registry rebuilds.

5. Click **Refresh** in the dashboard — the **robustness** server now appears with
   status `● ok` and **4 tools**.

That's it. The agent can now call `robustness__*` tools, and they're listed in the
Agent dashboard's MCP card.

> The same add works from **Settings → MCP** if you prefer that form; it stores the
> same config.

---

## Verify it works

Ask the agent (or run in chat):

> "Run robustness__adversarial_robustness_checklist for an sklearn model on tabular
> clinical data with high_stakes=true."

> "Compute robustness metrics from these predictions: clean [0,1,0,1],
> adversarial [1,1,0,1], true [0,1,0,1]."

Or confirm the server from the dashboard:

```bash
curl -s http://127.0.0.1:8765/api/mcp | python3 -m json.tool
# -> look for {"name": "robustness", "ok": true, "tools": [...4 tools...]}
```

---

## Enabling the full ART evaluation

`robustness__evaluate_sklearn_robustness` needs the Adversarial Robustness Toolbox.
It reports a clear error if it's missing. Install it in the running container:

```bash
docker compose exec fox pip install adversarial-robustness-toolbox
```

(Add it permanently to the image by appending it to `Dockerfile`'s `RUN pip install`
line and rebuilding.)

Even without ART, the other three tools — the metrics helper, the checklist, and
the FGSM demo — work with only `numpy`.

---

## Using the ART evaluation end-to-end

1. Train a small classifier and save it as a joblib model + `.npy` arrays
   (e.g. inside a notebook/project):

   ```python
   import joblib, numpy as np
   from sklearn.linear_model import LogisticRegression
   from sklearn.datasets import load_breast_cancer

   X, y = load_breast_cancer(return_X_y=True)
   X /= np.max(np.abs(X), axis=0)          # scale to keep eps meaningful
   m = LogisticRegression(max_iter=2000).fit(X[:400], y[:400])
   joblib.dump(m, "model.joblib")
   np.save("X_test.npy", X[400:])
   np.save("y_test.npy", y[400:])
   ```

2. Ask the agent:

   > "Run robustness__evaluate_sklearn_robustness on model.joblib with X_test.npy
   > and y_test.npy, attack ProjectedGradientDescent, eps 0.05, and save the report
   > as an artifact."

3. The agent returns clean accuracy, robust accuracy, ASR and accuracy drop, and
   you can attach the report as a provenance-linked artifact.

---

## Recommended evaluation workflow

1. **Define the threat model** → `robustness__adversarial_robustness_checklist`
2. **Clean baseline** → ordinary evaluation metrics
3. **Generate adversarial examples** → ART attack (FGSM / PGD)
4. **Compute metrics** → `robustness__robustness_metrics_from_predictions`
5. **Interpret & document** → attach the report as an artifact
6. **Decide** → adversarial training, threshold change, or restricted use

Combine with the **privacy** server's tools (`privacy__*`) when the model handles
sensitive data — robustness and privacy red-teaming complement each other.

## Limitations

- FGSM / PGD are white-box attacks (gradient access); black-box threats need
  different tools.
- High robust accuracy on one attack does **not** prove security against stronger
  or adaptive attacks.
- ART is an evaluation aid, not formal verification. Always document attack
  parameters (ε, norm, iterations) and library versions.
