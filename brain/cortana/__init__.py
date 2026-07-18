"""CORTANA Brain — the Python half of the voice-activated fleet intel bot.

Brain owns everything that requires judgement: wake word, STT, grammar,
gazetteer, incident engine, routing, discipline, TTS synthesis, and the
Discord text-side (slash commands, components, roles). The Rust `ears`
process owns the voice socket; the two meet over a framed Unix domain
socket (GDD §15).

See docs/INTERFACES.md for the cross-module contract and docs/GDD.md for
the full specification.
"""
