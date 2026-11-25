from nivult.ats.pagine_seo import _etichetta_datori, _nome_azienda


def test_public_portal_label_is_consistent():
    for slug in ("francetravail", "bundesanstellung", "arbetsformedlingen"):
        assert _etichetta_datori(slug, 1) == "1 public portal"
    assert _etichetta_datori("greenhouse", 1) == "1 company"
    assert _etichetta_datori("greenhouse", 2) == "2 companies"


def test_company_page_suffixes_are_removed():
    assert _nome_azienda("ATOMS Careers page", "atoms") == "ATOMS"
    assert _nome_azienda("Example Career Page", "example") == "Example"
    assert _nome_azienda("Example Jobs Site", "example") == "Example"
    assert _nome_azienda("Example Employment Portal", "example") == "Example"
    assert _nome_azienda("Careers page", "example") is None
