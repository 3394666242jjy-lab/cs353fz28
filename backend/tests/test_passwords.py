import pytest

from app.services.passwords import hash_password, verify_password


@pytest.fixture(scope="module")
def stored_hash():
    return hash_password(" synthetic-密码 ")


def test_password_matches_without_storing_plaintext(stored_hash):
    assert "synthetic" not in stored_hash
    assert verify_password(" synthetic-密码 ", stored_hash)


def test_wrong_password_and_trimmed_password_do_not_match(stored_hash):
    assert not verify_password("wrong", stored_hash)
    assert not verify_password("synthetic-密码", stored_hash)


def test_salts_make_identical_passwords_have_different_hashes(stored_hash):
    other_hash = hash_password(" synthetic-密码 ")
    assert other_hash != stored_hash
    assert verify_password(" synthetic-密码 ", other_hash)


@pytest.mark.parametrize(
    "stored_hash",
    [
        "",
        "plaintext",
        "scrypt-v2$" + "0" * 32 + "$" + "0" * 64,
        "scrypt-v1$" + "z" * 32 + "$" + "0" * 64,
        "scrypt-v1$" + "0" * 31 + "$" + "0" * 65,
    ],
)
def test_malformed_hashes_fail_closed(stored_hash):
    assert not verify_password("synthetic", stored_hash)


@pytest.mark.parametrize("password", ["", "x" * 4097, "\ud800"])
def test_invalid_password_input_cannot_be_hashed_or_verified(password, stored_hash):
    with pytest.raises(ValueError):
        hash_password(password)
    assert not verify_password(password, stored_hash)
