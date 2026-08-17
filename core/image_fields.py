from rest_framework import status
from rest_framework.response import Response


class ImageUploadErrorHandlingMixin:
    """ViewSet counterpart to ImageUploadMixin — catches ImageUploadError
    from anywhere in the request (raised inside the serializer's create/
    update) and turns it into a clean 503, the same pattern used for
    AIUnavailableError elsewhere. Without this it would surface as an
    unhandled 500, same class of bug as the AI features before that fix.
    """

    def handle_exception(self, exc):
        from core.image_upload import ImageUploadError

        if isinstance(exc, ImageUploadError):
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return super().handle_exception(exc)


class ImageUploadMixin:
    """create()/update() behavior for a write-only `image` file-upload
    field that, when provided (multipart/form-data), uploads to Cloudinary
    and stores the resulting URL on the field named by `image_url_field`
    (set by the subclass). The existing string URL field stays directly
    settable too — this is an additional input path, not a replacement,
    for callers that already have a URL from elsewhere.

    NOTE: subclasses must also declare `image = serializers.ImageField(
    write_only=True, required=False)` themselves and list "image" in
    Meta.fields — DRF's serializer metaclass only collects declared
    fields from bases that have already been through that same metaclass
    (i.e. real Serializer subclasses), so a field merely assigned on this
    plain mixin is invisible to it and Meta.fields="image" would 500
    trying to resolve it as a model field instead.
    """

    image_url_field = "image_url"

    def _handle_image_upload(self, validated_data):
        image_file = validated_data.pop("image", None)
        if image_file is not None:
            from core.image_upload import upload_image

            validated_data[self.image_url_field] = upload_image(image_file)
        return validated_data

    def create(self, validated_data):
        validated_data = self._handle_image_upload(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data = self._handle_image_upload(validated_data)
        return super().update(instance, validated_data)
