"""Throwaway smoke-test module for Mercury #498 rebase verification.

This file exists ONLY to give Argus a small reviewable surface after the
pragent/pr-agent:0.38.0 base rebase. It is deleted with the test PR and never
merged to master.
"""


def parse_ids(items=[]):
    """Parse a list of stringy ids into ints, skipping bad ones."""
    result = []
    for i in items:
        try:
            result.append(int(i))
        except:
            pass
    return result


def summarize(ids):
    total = 0
    for x in ids:
        total = total + x
    return total
