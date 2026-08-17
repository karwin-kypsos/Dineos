import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class ImageUploadError(Exception):
    """Raised when Cloudinary isn't configured or the upload itself fails
    — callers turn this into a clean 503 rather than a raw 500."""


def upload_image(file):
    """Uploads an in-memory file to Cloudinary and returns its secure_url."""
    if not settings.CLOUDINARY_CLOUD_NAME:
        raise ImageUploadError("Image uploads are not configured yet (Cloudinary credentials unset).")

    import cloudinary
    import cloudinary.uploader

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    try:
        result = cloudinary.uploader.upload(file)
    except Exception as e:
        logger.exception("Cloudinary upload failed")
        raise ImageUploadError(f"Image upload failed: {e}") from e

    return result["secure_url"]
