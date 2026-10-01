class MediaError(Exception):
    """A download or upload failure that can be shown to a user."""


class FileTooLarge(MediaError):
    pass


class DurationTooLong(MediaError):
    pass


class NoMedia(MediaError):
    pass


class NoAttachments(MediaError):
    """The post was fetched successfully and has no attached media."""


class AuthenticationRequired(NoMedia):
    pass
