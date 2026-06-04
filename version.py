"""farmwatch — версіонування.

Схема: MAJOR.MINOR
  MAJOR — вручну +1 лише для великих апдейтів (не баг-фікси).
  MINOR — кількість коммітів (git rev-list --count HEAD); +1 за кожен коміт.

CI рахує MINOR під час збірки і запікає сюди __version__ = MAJOR.<commit-count>.
Щоб підняти MAJOR — зміни число нижче вручну в окремому коміті.
"""
MAJOR = 2
__version__ = "2.15"
