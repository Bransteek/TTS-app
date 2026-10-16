from lector.content.segmenter import build_chapter_text, make_chunks, split_sentences

HARD_CAP = 600


def _build(*texts):
    return build_chapter_text([(t, None) for t in texts])


def test_dialogue_dash_kept():
    text, _, sentences = _build(
        "—¿Tienes una copia física del certificado?", "—Está en casa."
    )
    assert [s.text for s in sentences] == [
        "—¿Tienes una copia física del certificado?",
        "—Está en casa.",
    ]
    assert sentences[0].paragraph_index == 0
    assert sentences[1].paragraph_index == 1


def test_abbreviations_not_split():
    _, _, sentences = _build("El Sr. Park llegó. Dijo hola.")
    assert len(sentences) == 2
    assert sentences[0].text == "El Sr. Park llegó."


def test_initials_and_more_abbreviations_not_split():
    assert len(split_sentences("J. R. Tolkien escribió mucho. Fin.")) == 2
    assert len(split_sentences("Vea la pág. Siete del cap. Dos, etc. Luego sigue.")) == 1
    assert len(split_sentences("Lo dijo p. ej. Pedro. Luego se fue.")) == 2


def test_question_inside_sentence():
    _, _, sentences = _build("¿Vienes? —preguntó ella.")
    assert len(sentences) == 1


def test_splits_after_closing_quotes_and_ellipsis():
    assert len(split_sentences("Dijo «hola.» Luego se fue.")) == 2
    assert len(split_sentences("¡Corre! ¿Dónde? Aquí... Ya.")) == 4
    assert len(split_sentences("Siguió… y paró.")) == 1


def test_offsets_roundtrip():
    text, paragraphs, sentences = _build(
        "Hola. ¿Qué tal? —Bien, gracias.", "El Sr. Park llegó. «Dijo hola.» Fin."
    )
    assert sentences
    for s in sentences:
        assert text[s.start : s.end] == s.text
    for p in paragraphs:
        assert text[p.start : p.start + len(p.text)] == p.text
    assert [s.index for s in sentences] == list(range(len(sentences)))


def _many_paragraphs():
    paras = []
    for i in range(4):
        paras.append(" ".join(f"Frase número {i}{j} del párrafo." for j in range(7)))
    return _build(*paras)[2]


def test_chunks_respect_limits():
    sentences = _many_paragraphs()
    chunks = make_chunks("d", 0, sentences, max_chars=600, max_sentences=3)
    assert len(chunks[0].sentences) == 1
    for c in chunks:
        assert len(c.sentences) <= 3
        assert len(c.text) <= 600
        assert len({s.paragraph_index for s in c.sentences}) == 1
        assert c.paragraph_index == c.sentences[0].paragraph_index
    covered = [s.index for c in chunks for s in c.sentences]
    assert covered == [s.index for s in sentences]


def test_chunk_char_limit():
    sentences = _many_paragraphs()
    chunks = make_chunks("d", 0, sentences, max_chars=80, max_sentences=10)
    assert all(len(c.text) <= 80 or len(c.sentences) == 1 for c in chunks)


def test_long_sentence_is_split():
    words = ["palabra"] * 190
    long_sentence = ", ".join(" ".join(words[i : i + 5]) for i in range(0, 190, 5)) + "."
    assert len(long_sentence) >= 1500
    text, _, sentences = _build(long_sentence)
    assert len(sentences) > 1
    assert all(len(s.text) <= HARD_CAP for s in sentences)
    assert [s.index for s in sentences] == list(range(len(sentences)))
    for s in sentences:
        assert text[s.start : s.end] == s.text
    assert " ".join(s.text for s in sentences) == long_sentence


def test_long_sentence_without_spaces_is_hard_cut():
    text, _, sentences = _build("a" * 1500)
    assert all(len(s.text) <= HARD_CAP for s in sentences)
    assert "".join(s.text for s in sentences) == "a" * 1500


def test_chunks_start_at_first_sentence():
    sentences = _many_paragraphs()
    chunks = make_chunks("d", 0, sentences, 600, 3, first_sentence=5)
    assert chunks[0].sentences[0].index == 5
    assert len(chunks[0].sentences) == 1


def test_chunk_ids_stable():
    sentences = _many_paragraphs()
    a = [c.chunk_id for c in make_chunks("d", 0, sentences, 600, 3)]
    b = [c.chunk_id for c in make_chunks("d", 0, sentences, 600, 3)]
    assert a == b
    assert len(set(a)) == len(a)
