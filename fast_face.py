import numpy as np
import face_recognition
import cv2
from PIL import Image
from typing import Optional, Tuple, Union


def _pil_to_np(pil_img: Image.Image) -> np.ndarray:
    """Convert PIL image to RGB numpy array."""
    return np.array(pil_img.convert("RGB"))


def encode_face_fast(
    pil_img: Image.Image,
    scale: float = 1.0,
    model: str = "hog",
) -> Optional[np.ndarray]:
    """
    Compute a single face encoding from a PIL image, using downscaling for speed.

    - Returns None if no face is detected / encoded.
    - Uses HOG model by default (fast on CPU).
    """
    img_np = _pil_to_np(pil_img)

    # Optionally downscale for speed; scale=1.0 keeps full resolution for best accuracy.
    if scale != 1.0:
        proc_img = cv2.resize(img_np, (0, 0), fx=scale, fy=scale)
    else:
        proc_img = img_np

    # Detect faces
    locations = face_recognition.face_locations(proc_img, model=model)
    if not locations:
        return None

    # Compute encodings
    encodings = face_recognition.face_encodings(proc_img, known_face_locations=locations)
    if not encodings:
        return None

    return encodings[0]


def compare_encodings_fast(
    known_encodings: Union[np.ndarray, list],
    candidate_encoding: np.ndarray,
    tolerance: float = 0.5,
) -> Tuple[bool, float]:
    """
    Compare one candidate encoding to one or many known encodings.

    - known_encodings: shape (128,) or (N, 128)
    - Returns (is_match, best_distance)
    """
    known = np.asarray(known_encodings)
    if known.ndim == 1:
        known = known.reshape(1, -1)

    distances = face_recognition.face_distance(known, candidate_encoding)
    best_distance = float(distances.min())
    is_match = best_distance <= tolerance
    return is_match, best_distance


