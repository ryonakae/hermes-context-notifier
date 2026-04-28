"""Hermes plugin entry point for hermes-context-notifier."""

try:  # Hermes plugin loader imports this as a package module.
    from .hermes_context_notifier import register
except ImportError:  # Pytest may import root __init__.py as a plain module.
    from hermes_context_notifier import register

__all__ = ["register"]
