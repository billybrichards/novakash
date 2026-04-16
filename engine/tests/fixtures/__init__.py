"""Test fixture builders — explicit, importable.

Pattern: entity/VO builders live in domain.py, port fakes in ports.py,
infrastructure stubs (sessions, settings) in infra.py. Tests import
explicitly — no pytest magic discovery for these.
"""
