from service.answer_text import HeadingFixer, AnswerEdgeTrimmer


def test_whole_turns_headings_into_bold_labels():
    src = "### What I can do\n- a\n## **Bold** ##\n#### \nnot # a heading\n#tag\n"
    out = HeadingFixer.whole(src)
    assert out.splitlines()[0] == "**What I can do**"
    assert "**Bold**" in out and "****" not in out
    assert "not # a heading" in out and "#tag" in out
    assert "###" not in out


def test_fenced_code_untouched():
    src = "```\n# comment\n```\n# Title"
    assert HeadingFixer.whole(src) == "```\n# comment\n```\n**Title**"


def test_streamed_matches_whole_any_chunking():
    src = "Intro\n### What I can do\n- x\n## Next ##\nend"
    for n in (1, 2, 3, 7):
        f = HeadingFixer()
        out = "".join(f.feed(src[i:i + n]) for i in range(0, len(src), n)) + f.flush()
        assert out == HeadingFixer.whole(src)
        assert "#" not in out


def test_heading_text_streamed_never_shows_hashes_early():
    f = HeadingFixer()
    assert f.feed("##") == "" and f.feed("# Hi") == ""
    assert f.feed("\n") == "**Hi**\n"
