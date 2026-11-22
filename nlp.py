import spacy
import logging

nlp_spacy = None

def init_spacy():
    global nlp_spacy
    try:
        nlp_spacy = spacy.load("en_core_web_md")
    except Exception as e:
        logging.warning(f"No spaCy: {e}. Fallback heuristic NLP will be used.")
        nlp_spacy = None

def extract_spacy_entities(text):
    if not nlp_spacy: return None, None
    doc = nlp_spacy(text)
    verbs = [token.lemma_.upper() for token in doc if token.pos_ == "VERB"]
    objects = [token.text for token in doc if token.dep_ in ('dobj', 'nsubj', 'pobj', 'compound') and not token.is_stop]
    return verbs, " ".join(objects).title() if objects else None
