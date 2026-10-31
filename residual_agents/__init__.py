"""Residual Agents (RA): the planner-executor-critic orchestrator with an experience layer, as used in the paper
"Reimagining computational quantum research with AI scientists".

residual_agents/orchestrator.py is generated from fragments/ by tools/build.py and runs one episode per process; start it
with `ra run` (residual_agents/cli.py). Importing this package does not import the orchestrator.
"""
__version__ = "1.0.0"

# raw sha256 of the four memory files in residual_agents/memories/
MEMORY_SHA256 = {
    "planner_notes.md": "0fa9638f45f819544414a90510774fa227f547b8fae5f0e3f507da750f9408e9",
    "critic_notes.md": "efeb53597ab9c56f14e7ee1d9e3b039542ecb668501ee41fabed710729a9c0e3",
    "residual_bank.md": "acae19dbc182ed7c1119f862140ae64658f7405cc2ac8de30de7c9487c0af43e",
    "libraries.md": "31efd04c59ae5535c8f90cd66b813422db5652a59c9a1b1d84d5cf64bee23411",
}

# what the ledger event notes_loaded records for them: sha256 of the stripped text, first 8 hex digits
MEMORY_LEDGER_HASH = {
    "planner_notes": "28d0e828",
    "critic_notes": "c8521f77",
    "residual_bank": "24ca922e",
    "libraries": "a3a681e8",
}
