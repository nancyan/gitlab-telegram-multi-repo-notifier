from app.security import event_fingerprint, hash_secret, verify_secret


def test_secret_is_stored_as_hash_and_verified() -> None:
    stored = hash_secret("correct horse battery staple")

    assert stored != "correct horse battery staple"
    assert verify_secret("correct horse battery staple", stored)
    assert not verify_secret("wrong", stored)
    assert not verify_secret(None, stored)


def test_event_fingerprint_is_stable_per_repository() -> None:
    first = event_fingerprint("repo-one", b'{"object_kind":"push"}')
    second = event_fingerprint("repo-one", b'{"object_kind":"push"}')
    other_repo = event_fingerprint("repo-two", b'{"object_kind":"push"}')

    assert first == second
    assert first != other_repo
    assert first.startswith("generated-")
