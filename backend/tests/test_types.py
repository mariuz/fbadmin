from fbadmin.metadata.catalog import decode_trigger_type
from fbadmin.metadata.types import format_type, quote_ident


def test_numeric():
    assert format_type(16, 1, 8, 15, -2) == "NUMERIC(15,2)"
    assert format_type(8, 2, 4, 9, -3) == "DECIMAL(9,3)"
    assert format_type(26, 1, 16, 38, -4) == "NUMERIC(38,4)"


def test_chars():
    assert format_type(37, 0, 200, None, 0, 50, None, "UTF8", "UTF8") == "VARCHAR(50)"
    assert format_type(14, 0, 16, None, 0, 16, None, "OCTETS", "UTF8") == "CHAR(16) CHARACTER SET OCTETS"


def test_blob():
    assert format_type(261, 1, 8, None, 0, None, 80, "UTF8", "UTF8") == "BLOB SUB_TYPE TEXT"
    assert format_type(261, 0, 8, None, 0, None, 80) == "BLOB SUB_TYPE BINARY"


def test_simple_types():
    assert format_type(23) == "BOOLEAN"
    assert format_type(35) == "TIMESTAMP"
    assert format_type(29) == "TIMESTAMP WITH TIME ZONE"
    assert format_type(27) == "DOUBLE PRECISION"


def test_quote_ident():
    assert quote_ident("CUSTOMER") == "CUSTOMER"
    assert quote_ident("customer") == '"customer"'
    assert quote_ident("ORDER") == '"ORDER"'
    assert quote_ident("MY TABLE") == '"MY TABLE"'
    assert quote_ident('A"B') == '"A""B"'


def test_trigger_types():
    assert decode_trigger_type(1) == ("dml", "BEFORE INSERT")
    assert decode_trigger_type(2) == ("dml", "AFTER INSERT")
    assert decode_trigger_type(3) == ("dml", "BEFORE UPDATE")
    assert decode_trigger_type(6) == ("dml", "AFTER DELETE")
    assert decode_trigger_type(17) == ("dml", "BEFORE INSERT OR UPDATE")
    assert decode_trigger_type(113) == ("dml", "BEFORE INSERT OR UPDATE OR DELETE")
    assert decode_trigger_type(8192) == ("database", "ON CONNECT")
