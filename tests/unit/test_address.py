from mail_router.address import is_valid_ds_id, parse_recipient

DOMAINS = {"shipard.email"}


def test_hash_id_no_mailbox():
    p = parse_recipient("4l3j-z0bz-kz39-echj@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "4l3j-z0bz-kz39-echj"
    assert p.mailbox is None
    assert p.domain == "shipard.email"


def test_hash_id_with_plus_mailbox():
    p = parse_recipient("4l3j-z0bz-kz39-echj+invoices@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "4l3j-z0bz-kz39-echj"
    assert p.mailbox == "invoices"


def test_web_id_with_double_dash_mailbox():
    p = parse_recipient("firma-xyz--invoices@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma-xyz"
    assert p.mailbox == "invoices"


def test_first_separator_wins_plus_before_dash():
    p = parse_recipient("firma-xyz+a--b@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma-xyz"
    assert p.mailbox == "a--b"


def test_first_separator_wins_dash_before_plus():
    p = parse_recipient("firma-xyz--a+b@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma-xyz"
    assert p.mailbox == "a+b"


def test_empty_mailbox_becomes_none():
    p = parse_recipient("firma-xyz+@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma-xyz"
    assert p.mailbox is None


def test_unknown_domain():
    assert parse_recipient("firma-xyz@other.com", DOMAINS) is None


def test_missing_at():
    assert parse_recipient("no-at-sign", DOMAINS) is None


def test_invalid_ds_id_leading_dash():
    assert parse_recipient("-foo@shipard.email", DOMAINS) is None


def test_invalid_ds_id_double_dash_in_web_id():
    # 'firma--xyz' without mailbox → split at first -- so ds_id='firma', mailbox='xyz'
    p = parse_recipient("firma--xyz@shipard.email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma"
    assert p.mailbox == "xyz"


def test_empty_local_part():
    assert parse_recipient("@shipard.email", DOMAINS) is None


def test_case_normalization():
    p = parse_recipient("FIRMA-xyz+INV@Shipard.Email", DOMAINS)
    assert p is not None
    assert p.ds_id == "firma-xyz"
    assert p.mailbox == "INV"  # mailbox case preserved — matched on shpd side
    assert p.domain == "shipard.email"


def test_is_valid_ds_id_hash():
    assert is_valid_ds_id("4l3j-z0bz-kz39-echj")


def test_is_valid_ds_id_web():
    assert is_valid_ds_id("firma-xyz")
    assert is_valid_ds_id("a")
    assert is_valid_ds_id("a1")


def test_is_valid_ds_id_rejects():
    assert not is_valid_ds_id("")
    assert not is_valid_ds_id("-foo")
    assert not is_valid_ds_id("foo-")
    assert not is_valid_ds_id("foo--bar")
    assert not is_valid_ds_id("Foo")  # uppercase
    assert not is_valid_ds_id("a b")
