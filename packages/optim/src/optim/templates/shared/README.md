# Shared

What two or more of this repo's models use: datasets, rules, prep, and optimizer code such as trade sizing. It is laid
out like a model's folder, without `model.py`, `main.py` or `params/`, and models import from it, as in
`from models.shared.rules import controls`. A model never imports another model.

Move code here when a second model needs it, not before, and update the imports of the model it came from. Each model's
fixture still has a file for every dataset it reads, shared ones included. When a second repo needs a dataset, it
belongs in `optim.datasets` instead.

`uv run optim docs how-to/share-code-between-models` says more.
