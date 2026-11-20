"""What is sent to a cloud LLM must not contain the sensitive values; the response is restored locally."""

from pipeline.utils.anonymizer import Anonymizer

SAMPLE = (
    "Contact Jean at jean.dupont@acme-defense.fr or +33 6 12 34 56 78 (fixe 01 23 45 67 89). "
    "Server 192.168.10.25, IBAN FR76 3000 6000 0112 3456 7890 189, carte 4539 1488 0343 6467, "
    "NIR 1 85 05 78 006 084 36, key AKIAIOSFODNN7EXAMPLE. Projet ORION pour le client Acme Défense."
)


def test_nothing_sensitive_survives_in_the_sent_text() -> None:
    result = Anonymizer(["ORION", "Acme Défense", "acme-defense"]).anonymize(SAMPLE)
    for leaked in (
        "jean.dupont@acme-defense.fr", "+33 6 12 34 56 78", "01 23 45 67 89", "192.168.10.25",
        "FR76 3000 6000 0112 3456 7890 189", "4539 1488 0343 6467", "1 85 05 78 006 084 36",
        "AKIAIOSFODNN7EXAMPLE", "ORION", "Acme Défense",
    ):
        assert leaked not in result.text, leaked
    assert "Jean" in result.text  # LIMIT: a bare first name is not detected without sensitive_terms


def test_response_round_trip_restores_the_originals() -> None:
    anonymizer = Anonymizer(["ORION"])
    sent = anonymizer.anonymize("Le projet ORION écrit à a.b@corp.com. ORION est prioritaire.")
    assert sent.text.count("[TERM_1]") == 2  # same value -> same placeholder
    llm_answer = f"Résumé : {sent.text}"
    restored = Anonymizer.restore(llm_answer, sent.mapping)
    assert "ORION" in restored and "a.b@corp.com" in restored and "[TERM_1]" not in restored


def test_terms_match_whole_words_ignore_case_and_round_trip_exactly() -> None:
    text = "ORION, Orion, orionis, cassiorion, ORION"
    result = Anonymizer(["orion"]).anonymize(text)
    assert result.text == "[TERM_1], [TERM_2], orionis, cassiorion, [TERM_1]"
    assert Anonymizer.restore(result.text, result.mapping) == text


def test_a_term_inside_an_email_cannot_split_it_and_leak_the_local_part() -> None:
    result = Anonymizer(["Acme"]).anonymize("write to paul@acme.fr or ask Acme")
    assert "paul" not in result.text and "acme.fr" not in result.text
    assert result.text == "write to [EMAIL_1] or ask [TERM_1]"
    assert Anonymizer.restore(result.text, result.mapping) == "write to paul@acme.fr or ask Acme"


def test_longest_term_wins() -> None:
    result = Anonymizer(["Acme", "Acme Défense"]).anonymize("Client Acme Défense et Acme")
    assert result.text == "Client [TERM_1] et [TERM_2]"


def test_card_numbers_need_a_valid_luhn_checksum() -> None:
    result = Anonymizer().anonymize("ref 1234 5678 9012 3456 mais carte 4539 1488 0343 6467")
    assert "1234 5678 9012 3456" in result.text and "4539" not in result.text


def test_counts_describe_what_was_found() -> None:
    result = Anonymizer(["ORION"]).anonymize("ORION a@b.co c@d.co")
    assert result.counts == {"TERM": 1, "EMAIL": 2} and result.n_entities == 3


def test_plain_text_is_left_untouched() -> None:
    text = "Summarize the following paragraph in one sentence."
    result = Anonymizer().anonymize(text)
    assert result.text == text and result.n_entities == 0
