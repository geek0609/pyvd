import json

from internal.extractors.gallery import _marked_nsfw


def test_gallery_metadata_detects_marked_posts(tmp_path) -> None:
    sidecar = tmp_path / "post.json"
    sidecar.write_text(json.dumps({"over_18": True}))
    assert _marked_nsfw(tmp_path)
    sidecar.write_text(json.dumps({"crosspost": {"possibly_sensitive": True}}))
    assert _marked_nsfw(tmp_path)
    sidecar.write_text(json.dumps({"over_18": False, "age_limit": 0}))
    assert not _marked_nsfw(tmp_path)
