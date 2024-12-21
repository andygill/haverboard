from tests.test_utils import Content


def test_docs_first_example():
    assert (
        Content("examples/first_example/main.py")
        == Content("examples/first_example/README.md")[4:11]
    )
    assert (
        Content("tests/e2e/test_e2e_haverscript/test_first_example.txt")[2:]
        == Content("examples/first_example/README.md")[16:40]
    )


def test_validate_example():
    assert (
        Content("examples/validate/main.py")
        == Content("examples/validate/README.md")[4:25]
    )
    assert (
        Content("tests/e2e/test_e2e_haverscript/test_validate.txt")[2:]
        == Content("examples/validate/README.md")[31:52]
    )
