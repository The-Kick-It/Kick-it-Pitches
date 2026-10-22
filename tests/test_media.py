from kickit_crawler.media import NOT_A_PHOTO, references_from_tags


def test_references_from_tags():
    titles, qids, urls = references_from_tags({
        "wikimedia_commons": "Category:Stade Mohammed V;File:Pitch.jpg",
        "image": "https://commons.wikimedia.org/wiki/File:Stade_Pere_Jego.jpg;https://example.com/padel.jpg",
        "wikidata": "Q123",
    })
    assert titles == ["Category:Stade Mohammed V", "File:Pitch.jpg", "File:Stade Pere Jego.jpg"]
    assert qids == ["Q123"]
    assert urls == ["https://example.com/padel.jpg"]


def test_bare_commons_value_is_a_file_and_bad_wikidata_is_ignored():
    titles, qids, _ = references_from_tags({"wikimedia_commons": "Terrain.jpg", "wikidata": "not-an-id"})
    assert titles == ["File:Terrain.jpg"]
    assert qids == []


def test_logos_and_maps_are_not_photos():
    assert NOT_A_PHOTO.search("File:Tangier Grand Stadium logo.jpg")
    assert NOT_A_PHOTO.search("File:Stade plan.png")
    assert not NOT_A_PHOTO.search("File:Tangier Stadium.jpg")
