import os
import uuid

from fastapi import UploadFile

from data_service.configs.static_path_config import FLOORPLAN_IMAGE_DIR


def save_floorplan_image_to_static(images: list[UploadFile]) -> list[str]:
    """
    Save floorplan image(s) to the static directory.
    Filenames are uuid-based only (no id prefix), since a floorplan_id doesn't
    exist yet at upload time during create.

    Args:
        images: List of uploaded image files (typically a single image).

    Returns:
        List of saved image filenames.
    """
    image_names = []
    if not os.path.exists(FLOORPLAN_IMAGE_DIR):
        os.makedirs(FLOORPLAN_IMAGE_DIR)

    for image in images:
        image_name = f"{uuid.uuid4().hex}.{image.filename.split('.')[-1]}"
        image_names.append(image_name)
        file_path = os.path.join(FLOORPLAN_IMAGE_DIR, image_name)
        with open(file_path, "wb") as f:
            f.write(image.file.read())
    return image_names


def delete_floorplan_image_from_static_by_name(image_name: str) -> None:
    """
    Delete a specific floorplan image from the static directory by filename.

    Args:
        image_name: Filename of the image to delete.
    """
    file_path = os.path.join(FLOORPLAN_IMAGE_DIR, image_name)
    if os.path.exists(file_path):
        os.remove(file_path)
    else:
        print(f"⚠️ File {file_path} does not exist.")