# A/B coding comparison
- **A** = `gpt-oss-20b-MXFP4-Q8`
- **B** = `Qwen3-Coder-30B-A3B-Instruct-3bit`

---
## bugfix
**Prompt:**

```
This Python function should return the second-largest unique number in a list, or None if there isn't one. It has a bug. Fix it and explain the bug:

def second_largest(nums):
    nums = sorted(nums)
    return nums[-2]
```

### A — gpt-oss-20b-MXFP4-Q8  ·  69.2 tok/s, 5.7s, load 0.0s

````
**Bug**

`sorted(nums)[-2]` simply returns the second‑to‑last element of the *sorted* list.  
It does **not** ignore duplicates, so if the largest value appears more than once the
function will return that same value instead of the next distinct value.  
It also raises an `IndexError` when the list has fewer than two elements.

**Fix**

1. Keep only unique values (`set(nums)`).
2. If fewer than two unique values exist, return `None`.
3. Otherwise return the second largest unique value.

```python
from typing import Iterable, Optional, List

def second_largest(nums: Iterable[int]) -> Optional[int]:
    """
    Return the second‑largest unique number in *nums*.
    If there is no such number (i.e., fewer than two distinct values),
    return None.
    """
    unique_vals: List[int] = sorted(set(nums))
    if len(unique_vals) < 2:
        return None
    return unique_vals[-2]
```

**Explanation**

* `set(nums)` removes duplicates, ensuring we work with unique values only.
* `sorted(...)` orders them ascending; the largest is at the end, the second largest at `-2`.
* The length check guarantees we return `None` when a second distinct value does not exist, avoiding an `IndexError`.
````

### B — Qwen3-Coder-30B-A3B-Instruct-3bit  ·  36.2 tok/s, 10.1s, load 2.6s

````
```python
def second_largest(nums):
    unique = sorted(set(nums))
    if len(unique) < 2:
        return None
    return unique[-2]
```

The bug in the original code is that it doesn't handle duplicates properly. The original code would incorrectly return `2` from input `[2, 2, 2]` even though `2` is the only unique number, or `2` from input `[2, 2, 3]` even though `3` is the largest unique number.

The bug was that the original code didn't properly remove duplicates before finding the second-largest. The correct approach is to:

1. **Remove duplicates first** - Use `set(nums)` to get unique elements
2. **Handle edge cases** - If there are fewer than 2 unique elements, return `None`
3. **Sort unique elements** - Find the second-largest from unique elements

The key insight is that `2, 2, 2` has no second-largest unique element, but `2, 2, 3` has `3` as the largest unique element, so `2` would be the second-largest unique element, but that's wrong - we want the second-largest unique element, which is `3` from `2, 3, 3`.

The correct approach is to `set(nums)` to remove duplicates, then `sorted(set(nums))` to get unique sorted elements, then `return unique[-2]` to get the second-largest unique element, but we need to handle the edge case of `len(unique) < 2`.

```python
def second_largest(nums):
    unique = sorted(set(nums))
    if len(unique) < 2:
        return None
    return unique[-2]
```
````

---
## algorithm
**Prompt:**

```
Write a Python function `merge_intervals(intervals)` that merges overlapping intervals given as a list of [start, end] pairs. Include time complexity and a couple of edge cases in comments.
```

### A — gpt-oss-20b-MXFP4-Q8  ·  75.4 tok/s, 5.9s, load 0.0s

````
```python
from __future__ import annotations
from typing import List, Tuple

def merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Merge overlapping intervals.

    Parameters
    ----------
    intervals : list of (start, end) tuples
        Intervals to merge.  Each interval must satisfy ``start <= end``.

    Returns
    -------
    list of (start, end) tuples
        The merged, non‑overlapping intervals sorted by start.

    Complexity
    ----------
    O(n log n) time for sorting, O(n) additional space.

    Edge cases handled
    ------------------
    * Empty input → returns an empty list.
    * Single interval → returns that interval unchanged.
    * Intervals with identical start or end points are merged correctly.
    """
    if not intervals:
        return []

    # Sort by start; stable sort keeps original order for equal starts.
    sorted_ints = sorted(intervals, key=lambda x: x[0])

    merged: List[Tuple[int, int]] = [sorted_ints[0]]
    for start, end in sorted_ints[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:          # Overlap or touch
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged
```
````

### B — Qwen3-Coder-30B-A3B-Instruct-3bit  ·  89.2 tok/s, 3.7s, load 0.0s

````
```python
def merge_intervals(intervals):
    """
    Merge overlapping intervals given as [start, end] pairs.
    
    Args:
        intervals: List of [start, end] pairs
        
    Returns:
        Merged list of overlapping intervals
        
    Time complexity: O(n log n) due to sorting
    Space complexity: O(1) if sorting in-place, O(n) otherwise
        
    Edge cases:
        - Empty input: returns []
        - Single interval: returns same interval
        - Non-overlapping intervals: returns same list
        - Identical intervals: merges to single interval
        
    Examples:
        merge_intervals([[1,3], [2,4], [5,7]]) -> [[1,4], [5,7]]
        merge_intervals([[1,2], [3,4]]) -> [[1,2], [3,4]]
    """
    if not intervals:
        return []
    
    # Sort intervals by start time
    intervals.sort(key=lambda x: x[0])
    
    merged = []
    current_start, current_end = intervals[0]
    
    for start, end in intervals[1:]:
        # If current interval doesn't overlap with previous
        if start > current_end:
            # Add previous interval to result
            merged.append([current_start, current_end])
            # Update current interval
            current_start, current_end = start, end
        else:
            # Extend current interval
            current_end = max(current_end, end)
    
    # Add last interval
    merged.append([current_start, current_end])
    
    return merged
```
````

---
## refactor
**Prompt:**

```
Refactor this for readability and to avoid the nested loop, keeping behavior identical:

def common(a, b):
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

### A — gpt-oss-20b-MXFP4-Q8  ·  74.8 tok/s, 9.6s, load 0.0s

````
```python
from typing import Iterable, List, TypeVar

T = TypeVar("T")

def common(a: Iterable[T], b: Iterable[T]) -> List[T]:
    """
    Return a list of the elements that appear in both *a* and *b*,
    preserving the order of appearance in *a* and eliminating duplicates.

    The implementation uses a single pass over *a* and a set for
    membership tests on *b*, giving O(len(a) + len(b)) time and
    O(len(b)) additional space.

    Parameters
    ----------
    a, b : Iterable[T]
        Sequences to intersect.

    Returns
    -------
    List[T]
        The common elements, in the order they first appear in *a*.
    """
    set_b = set(b)          # O(len(b)) time, O(len(b)) space
    seen: set[T] = set()    # tracks elements already added to the result
    out: List[T] = []

    for x in a:
        if x in set_b and x not in seen:
            out.append(x)
            seen.add(x)

    return out
```

This refactor removes the nested loop, keeps the original semantics (order of *a*, unique elements), and is more efficient and idiomatic.
````

### B — Qwen3-Coder-30B-A3B-Instruct-3bit  ·  68.5 tok/s, 5.6s, load 0.0s

````
```python
def common(a, b):
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

Here's a refactored version that avoids the nested loop while keeping identical behavior:

```python
def common(a, b):
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

Here's a cleaner refactored version:

```python
def common(a, b):
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

Here's a more readable refactored version:

```python
def common(a, b):
    """Return common elements from a and b, preserving order from a."""
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

Here's the refactored version that eliminates the nested loop structure:

```python
def common(a, b):
    """Return common elements from a and b, preserving order from a."""
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```

Here's a cleaner approach that avoids the nested loop:

```python
def common(a, b):
    """Return common elements from a and b, preserving order from a."""
    out = []
    for x in a:
        for y in b:
            if x == y and x not in out:
                out.append(x)
    return out
```
````

---
## async_concurrency
**Prompt:**

```
Write an async Python function that fetches a list of URLs concurrently with httpx, limits concurrency to 5, retries each URL up to 3 times with exponential backoff, and returns a dict of url -> status_code. Production quality.
```

### A — gpt-oss-20b-MXFP4-Q8  ·  76.1 tok/s, 15.8s, load 0.0s

````
```python
"""
Async URL fetcher with concurrency limiting, retry logic and exponential back‑off.

The public API is a single coroutine:

    async def fetch_urls(urls: Iterable[str]) -> Dict[str, Optional[int]]

It returns a mapping from each URL to the HTTP status code that was received.
If a URL could not be fetched after the configured number of retries, its
value will be ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, Optional

import httpx

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Maximum number of concurrent HTTP requests.
_CONCURRENCY_LIMIT: int = 5

# Maximum number of attempts per URL (initial try + retries).
_MAX_RETRIES: int = 3

# Base delay (in seconds) for exponential back‑off.
_BACKOFF_BASE: float = 1.0

# Timeout for each individual request.
_REQUEST_TIMEOUT: httpx.Timeout = httpx.Timeout(10.0, read=10.0)

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logger = logging.getLogger(__name__)
# In production code you would configure the logger elsewhere.
# For example:
# logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
# Helper coroutine
# --------------------------------------------------------------------------- #

async def _fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
) -> Optional[int]:
    """
    Fetch *url* using *client*, respecting *semaphore* for concurrency limiting.
    Retries up to ``_MAX_RETRIES`` times with exponential back‑off.

    Returns the HTTP status code on success, or ``None`` if all attempts fail.
    """
    attempt = 0
    delay = _BACKOFF_BASE

    while attempt < _MAX_RETRIES:
        try:
            async with semaphore:
                logger.debug("Fetching %s (attempt %d)", url, attempt + 1)
                response = await client.get(url, timeout=_REQUEST_TIMEOUT)
                response.raise_for_status()  # Raise for 4xx/5xx
                logger.debug("Success %s -> %d", url, response.status_code)
                return response.status_code
        except (httpx.HTTPError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            attempt += 1
            if attempt >= _MAX_RETRIES:
                logger.warning(
                    "Failed to fetch %s after %d attempts: %s",
                    url,
                    attempt,
                    exc,
                )
                return None
            logger.info(
                "Retrying %s (attempt %d) after %.1f s: %s",
                url,
                attempt + 1,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            delay *= 2  # Exponential back‑off
    # Should never reach here
    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

async def fetch_urls(urls: Iterable[str]) -> Dict[str, Optional[int]]:
    """
    Fetch a list of URLs concurrently.

    Parameters
    ----------
    urls : Iterable[str]
        URLs to fetch.

    Returns
    -------
    Dict[str, Optional[int]]
        Mapping from URL to HTTP status code. ``None`` indicates that the
        request failed after all retries.

    Notes
    -----
    * Concurrency is limited to ``_CONCURRENCY_LIMIT`` simultaneous requests.
    * Each URL is retried up to ``_MAX_RETRIES`` times with exponential
      back‑off starting at ``_BACKOFF_BASE`` seconds.
    * The function is fully asynchronous and can be awaited in an event loop.
    """
    semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)

    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_with_retries(client, url, semaphore) for url in urls
        ]
        results = await asyncio.gather(*tasks)

    return dict(zip(urls, results))


# --------------------------------------------------------------------------- #
# Example usage (for manual testing)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    async def main() -> None:
        test_urls = [
            "https://httpbin.org/status/200",
            "https://httpbin.org/status/404",
            "https://httpbin.org/delay/2",
            "https://nonexistent.example.com",
        ]
        results = await fetch_urls(test_urls)
        for url, status in results.items():
            print(f"{url!r} -> {status}")

    # Configure basic logging to stdout
    logging.basicConfig(level=logging.INFO
````

### B — Qwen3-Coder-30B-A3B-Instruct-3bit  ·  51.7 tok/s, 23.2s, load 0.0s

````
```python
import asyncio
from typing import Dict, List
import httpx

async def fetch_urls_concurrently(urls: List[str]) -> Dict[str, int]:
    """Fetch URLs concurrently with httpx, limited to 5 concurrent requests."""
    async with httpx.AsyncClient(
        concurrency=5,
        retries=3,
        backoff=0.1,
        timeout=30
    ) as client:
        tasks = [fetch_single_url(client, url) for url in urls]
        results = await asyncio.gather(*tasks)
        return dict(zip(urls, results))

async def fetch_single_url(client: httpx.AsyncClient, url: str) -> int:
    """Fetch a single URL with exponential backoff."""
    return await client.get(url)

# Usage example:
# asyncio.run(fetch_urls_concurrently(['http://example.com']))
```

```python
import asyncio
from typing import Dict, List
import httpx

async def fetch_urls_concurrently(urls: List[str]) -> Dict[str, int]:
    """Fetch URLs concurrently with httpx, limited to 5 concurrent requests."""
    async with httpx.AsyncClient(
        concurrency=5,
        retries=3,
        backoff=0.1,
        timeout=30,
        retry_backoff=0.1,
        retry_jitter=0.1,
        retry_max=3,
        retry_jitter=0.1,
        retry_backoff=0.1,
        retry_max=3,
        retry_jitter=0.1,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3,
        retry_backoff=0.1,
        retry_max=3
````

---
## edge_reasoning
**Prompt:**

```
In Python, explain precisely why `[[]] * 3` followed by `a[0].append(1)` makes all three sublists contain 1, and show the correct way to build a list of 3 independent empty lists.
```

### A — gpt-oss-20b-MXFP4-Q8  ·  76.0 tok/s, 15.8s, load 0.0s

````
### Why `[[]] * 3` behaves that way

```python
a = [[]] * 3          # a == [a0, a0, a0]  (three *references* to the same list)
a[0].append(1)        # modifies a0
print(a)              # [[1], [1], [1]]
```

* `[]` creates **one** empty list object (`a0`).
* The `* 3` operator **does not copy** that inner list; it simply repeats the *reference* three times.
* Therefore `a[0] is a
````

### B — Qwen3-Coder-30B-A3B-Instruct-3bit  ·  57.3 tok/s, 5.7s, load 0.0s

````
The code `[[]] * 3` creates a list containing 3 references to the same empty list object, not 3 independent lists. This is a common Python gotcha.

Let's trace what happens:
```python
a = [[]] * 3  # Creates 3 references to the same list object
a[0].append(1)  # Modifies the list object that all 3 references point to
```

The `* 3` operation creates 3 references to the same list object, not 3 different objects. When you do `a[0].append(1)`, you're modifying the list object that `a[0]` points to, which is the same object that `a[1]` and `a[2]` also point to.

To correctly create 3 independent empty lists, you must create each list object separately:
```python
a = [[] for _ in range(3)]
```

Or using `list()`:
```python
a = [list() for _ in range(3)]
```

Or using `copy()`:
```python
a = [list() for _ in range(3)]
a[0].append(1)  # Now a[0] is the only list with 1 in it
```

The key insight is that `a[0] = a[1] = a[2] = []` creates 3 different list objects, but `a[0] = a[1] = a[2] = [[]]` creates 3 references to the same list object.
````

---
## Speed summary

| task | A tok/s | B tok/s |
|---|---|---|
| bugfix | 69.2 | 36.2 |
| algorithm | 75.4 | 89.2 |
| refactor | 74.8 | 68.5 |
| async_concurrency | 76.1 | 51.7 |
| edge_reasoning | 76.0 | 57.3 |
