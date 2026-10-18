"""Seed/key algorithm of CANExplorer's simulated ECU (UdsServer): every seed byte XOR 0xFF.

Choose this file as the key file in the Diagnostics view to unlock the simulated ECU. A real ECU needs its
manufacturer's algorithm.
"""


def compute_key(level: int, seed: bytes) -> bytes:
    return bytes(byte ^ 0xFF for byte in seed)
