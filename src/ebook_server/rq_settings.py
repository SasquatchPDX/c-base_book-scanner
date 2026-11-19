"""Django bootstrap imported by production and integration RQ workers."""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
# One worker process per core (#195 §8.2 item 6): keep NumPy/BLAS and OpenCV
# single-threaded inside each worker so N workers never run N x cores threads.
for _variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_variable, os.environ.get("BOOK_SCANNER_WORKER_THREADS", "1"))
django.setup()

import cv2  # noqa: E402  (after the thread environment is set)

cv2.setNumThreads(int(os.environ.get("BOOK_SCANNER_WORKER_THREADS", "1")))
