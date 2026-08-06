"""50 additional 'comprehensive'/agentic-flavored coding problems, extending
tests/bench50_data.py's 50 basics to a 100-problem suite (see
tests/bench100_data.py). Adds categories the first 50 didn't cover: OOP/data
structures, generators/decorators/closures, regex/parsing, graph & tree
algorithms, exception handling, and a few async problems.

Problems whose correctness can't be captured by one function call + one
expected value (a class needs a sequence of stateful calls; a generator/
decorator needs to be driven; some problems have multiple valid correct
answers) use `custom_check(ns) -> (passed, error)` instead of `tests`, with
full access to the exec'd namespace. See `check()` in bench50_data.py.
"""
from __future__ import annotations

import asyncio
import concurrent.futures

from benchmarks.bench50_data import CODE_INSTR


def _run_coro(coro):
    """Run `coro` to completion and return its result, regardless of whether
    we're already inside a running event loop. The benchmark runner itself
    is one big `asyncio.run(main())`, so a plain `asyncio.run(coro)` here
    would raise "cannot be called from a running event loop" — dodge that
    by running it in a fresh event loop on a separate thread instead."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no loop running here — safe to use directly
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _fail(msg: str) -> tuple[bool, str]:
    return False, msg


def _ok() -> tuple[bool, str]:
    return True, ""


PROBLEMS: list[dict] = [
    # ---------------- G. data structures & OOP ----------------
    dict(id="stack_class",
         prompt="Write a Python class `Stack` with methods `push(x)`, `pop()` "
                "(removes and returns the top item), `peek()` (returns top item "
                "without removing), `is_empty()`, and `size()`." + CODE_INSTR,
         custom_check=lambda ns: (
             lambda S=ns["Stack"](): (
                 S.push(1), S.push(2), S.push(3),
                 _ok() if (S.size() == 3 and S.peek() == 3 and S.pop() == 3
                           and S.size() == 2 and not S.is_empty())
                 else _fail("stack behavior wrong after push 1,2,3")
             )[-1]
         )(),
         ),
    dict(id="queue_class",
         prompt="Write a Python class `Queue` with methods `enqueue(x)`, "
                "`dequeue()` (removes and returns the front item, FIFO order), "
                "`is_empty()`, and `size()`." + CODE_INSTR,
         custom_check=lambda ns: (
             lambda Q=ns["Queue"](): (
                 Q.enqueue(1), Q.enqueue(2), Q.enqueue(3),
                 _ok() if (Q.dequeue() == 1 and Q.size() == 2 and Q.dequeue() == 2
                           and not Q.is_empty())
                 else _fail("queue did not preserve FIFO order")
             )[-1]
         )(),
         ),
    dict(id="lru_cache_class",
         prompt="Write a Python class `LRUCache(capacity)` implementing a "
                "least-recently-used cache with `get(key)` (returns value or -1 "
                "if missing, and marks key as recently used) and `put(key, "
                "value)` (evicts the least-recently-used entry if over "
                "capacity)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (c := ns["LRUCache"](2)),
             c.put(1, 1), c.put(2, 2),
             (r1 := c.get(1)),      # 1, marks 1 as recently used
             c.put(3, 3),            # evicts 2 (least recently used)
             (r2 := c.get(2)),      # -1, evicted
             c.put(4, 4),            # evicts 1
             (r3 := c.get(1)),      # -1, evicted
             (r4 := c.get(3)),      # 3
             (r5 := c.get(4)),      # 4
             _ok() if (r1, r2, r3, r4, r5) == (1, -1, -1, 3, 4)
             else _fail(f"LRU eviction order wrong: got {(r1,r2,r3,r4,r5)}, "
                        f"expected (1, -1, -1, 3, 4)")
         )[-1])()),
    dict(id="bank_account_class",
         prompt="Write a Python class `BankAccount(balance=0)` with methods "
                "`deposit(amount)`, `withdraw(amount)` (raises `ValueError` if "
                "amount exceeds balance, otherwise deducts it), and a `balance` "
                "attribute reflecting the current balance." + CODE_INSTR,
         custom_check=lambda ns: _run_bank_check(ns)),
    dict(id="linked_list_class",
         prompt="Write a Python class `LinkedList` with methods `append(x)` "
                "(adds to the end) and `to_list()` (returns a plain Python list "
                "of the elements in order). Implement it with your own node "
                "structure internally." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (ll := ns["LinkedList"]()),
             ll.append(1), ll.append(2), ll.append(3),
             _ok() if ll.to_list() == [1, 2, 3]
             else _fail(f"to_list() returned {ll.to_list()!r}, expected [1, 2, 3]")
         )[-1])()),
    dict(id="bst_class",
         prompt="Write a Python class `BST` (binary search tree) with methods "
                "`insert(x)`, `contains(x)` (returns True/False), and "
                "`inorder()` (returns a list of elements in sorted order)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (t := ns["BST"]()),
             [t.insert(x) for x in [5, 3, 8, 1, 4, 7, 9]],
             (ok1 := t.inorder() == [1, 3, 4, 5, 7, 8, 9]),
             (ok2 := t.contains(4) is True),
             (ok3 := t.contains(6) is False),
             _ok() if (ok1 and ok2 and ok3)
             else _fail(f"BST wrong: inorder={t.inorder()!r}, "
                        f"contains(4)={t.contains(4)!r}, contains(6)={t.contains(6)!r}")
         )[-1])()),
    dict(id="min_heap_class",
         prompt="Write a Python class `MinHeap` with methods `push(x)`, "
                "`pop()` (removes and returns the smallest element), and "
                "`peek()` (returns the smallest element without removing)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (h := ns["MinHeap"]()),
             [h.push(x) for x in [5, 3, 8, 1, 9]],
             (ok1 := h.peek() == 1),
             (popped := [h.pop() for _ in range(5)]),
             _ok() if (ok1 and popped == [1, 3, 5, 8, 9])
             else _fail(f"heap wrong: peek gave {ok1!r}, pop order {popped!r}, "
                        f"expected [1, 3, 5, 8, 9]")
         )[-1])()),
    dict(id="trie_class",
         prompt="Write a Python class `Trie` with methods `insert(word)`, "
                "`search(word)` (True only if the exact word was inserted), and "
                "`starts_with(prefix)` (True if any inserted word has this "
                "prefix)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (t := ns["Trie"]()),
             [t.insert(w) for w in ["apple", "app", "application"]],
             (o1 := t.search("app") is True),
             (o2 := t.search("appl") is False),
             (o3 := t.starts_with("appl") is True),
             (o4 := t.starts_with("banana") is False),
             _ok() if (o1 and o2 and o3 and o4)
             else _fail(f"trie wrong: search(app)={o1}, search(appl)={o2}, "
                        f"starts_with(appl)={o3}, starts_with(banana)={o4}")
         )[-1])()),
    dict(id="multiset_class",
         prompt="Write a Python class `MultiSet` with methods `add(x)`, "
                "`remove(x)` (removes one occurrence; no error if absent), and "
                "`count(x)` (returns how many occurrences are currently "
                "present)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (m := ns["MultiSet"]()),
             m.add(1), m.add(1), m.add(2),
             (o1 := m.count(1) == 2),
             m.remove(1),
             (o2 := m.count(1) == 1),
             m.remove(5),
             (o3 := m.count(2) == 1),
             _ok() if (o1 and o2 and o3)
             else _fail("multiset counts wrong after add/remove sequence")
         )[-1])()),
    dict(id="graph_class",
         prompt="Write a Python class `Graph` (undirected) with methods "
                "`add_edge(u, v)` and `bfs(start)` (returns a list of nodes in "
                "breadth-first order starting at `start`, visiting neighbors in "
                "the order edges were added)." + CODE_INSTR,
         custom_check=lambda ns: (lambda: (
             (g := ns["Graph"]()),
             g.add_edge("A", "B"), g.add_edge("A", "C"), g.add_edge("B", "D"),
             g.add_edge("C", "D"),
             (order := g.bfs("A")),
             _ok() if (order[0] == "A" and set(order) == {"A", "B", "C", "D"}
                       and len(order) == 4)
             else _fail(f"bfs('A') returned {order!r}, expected a BFS "
                        f"ordering of all 4 nodes starting at A")
         )[-1])()),

    # ---------------- H. generators / decorators / closures ----------------
    dict(id="fibonacci_generator",
         prompt="Write a Python generator function `fibonacci_generator(n)` "
                "that yields the first n Fibonacci numbers (0, 1, 1, 2, ...)." + CODE_INSTR,
         custom_check=lambda ns: (lambda got=list(ns["fibonacci_generator"](8)): (
             _ok() if got == [0, 1, 1, 2, 3, 5, 8, 13]
             else _fail(f"list(fibonacci_generator(8)) = {got!r}, expected "
                        f"[0, 1, 1, 2, 3, 5, 8, 13]")
         ))(),
         ),
    dict(id="make_counter",
         prompt="Write a Python function `make_counter()` that returns a new "
                "function (a closure) which, each time it's called with no "
                "arguments, returns the next integer starting from 1 (1, 2, 3, "
                "...). Each call to `make_counter()` must start its own "
                "independent counter." + CODE_INSTR,
         custom_check=lambda ns: (lambda c1=ns["make_counter"](), c2=ns["make_counter"](): (
             (seq1 := [c1(), c1(), c1()]),
             (seq2 := [c2()]),
             _ok() if (seq1 == [1, 2, 3] and seq2 == [1])
             else _fail(f"counter sequences wrong: c1 gave {seq1!r} "
                        f"(expected [1,2,3]), independent c2 gave {seq2!r} "
                        f"(expected [1])")
         )[-1])(),
         ),
    dict(id="memoize_decorator",
         prompt="Write a Python decorator `memoize` that caches a function's "
                "return value per distinct set of arguments, so repeated calls "
                "with the same arguments don't recompute." + CODE_INSTR,
         custom_check=lambda ns: _check_memoize(ns)),
    dict(id="retry_decorator",
         prompt="Write a Python decorator factory `retry(times)` that, when "
                "applied to a function, calls it and if it raises an exception, "
                "retries up to `times` total attempts before letting the last "
                "exception propagate. Returns the function's result on first "
                "success." + CODE_INSTR,
         custom_check=lambda ns: _check_retry(ns)),
    dict(id="compose_functions",
         prompt="Write a Python function `compose(*funcs)` that returns a new "
                "function applying the given functions right-to-left, i.e. "
                "`compose(f, g)(x) == f(g(x))`." + CODE_INSTR,
         custom_check=lambda ns: (lambda fn=ns["compose"](lambda x: x + 1, lambda x: x * 2): (
             _ok() if fn(3) == 7 else _fail(f"compose(add1, times2)(3) = {fn(3)!r}, expected 7")
         ))(),
         ),
    dict(id="chain_iterables",
         prompt="Write a Python generator function `chain_iterables(*iterables)` "
                "that yields all items from each iterable in turn, in order "
                "(like itertools.chain, but write it yourself without using "
                "itertools.chain)." + CODE_INSTR,
         custom_check=lambda ns: (lambda got=list(ns["chain_iterables"]([1, 2], (3, 4), [5])): (
             _ok() if got == [1, 2, 3, 4, 5]
             else _fail(f"list(chain_iterables([1,2],(3,4),[5])) = {got!r}, "
                        f"expected [1, 2, 3, 4, 5]")
         ))(),
         ),
    dict(id="running_total_generator",
         prompt="Write a Python generator function `running_total_generator(nums)` "
                "that yields the cumulative (running) sum after each element of "
                "nums." + CODE_INSTR,
         custom_check=lambda ns: (lambda got=list(ns["running_total_generator"]([1, 2, 3, 4])): (
             _ok() if got == [1, 3, 6, 10]
             else _fail(f"list(running_total_generator([1,2,3,4])) = {got!r}, "
                        f"expected [1, 3, 6, 10]")
         ))(),
         ),
    dict(id="once_decorator",
         prompt="Write a Python decorator `once` that ensures the decorated "
                "function's body only actually executes on the first call; "
                "every call (first and subsequent) returns the result from "
                "that first execution, and later calls must NOT re-run the "
                "function body." + CODE_INSTR,
         custom_check=lambda ns: _check_once(ns)),

    # ---------------- I. regex & parsing ----------------
    dict(id="extract_emails", fn_name="extract_emails",
         prompt="Write a Python function `extract_emails(text)` that returns a "
                "list of all email addresses found in `text`, in the order they "
                "appear." + CODE_INSTR,
         tests=[(("Contact us at help@example.com or sales@example.org, "
                  "no email here.",), ["help@example.com", "sales@example.org"]),
                (("no emails at all",), [])]),
    dict(id="is_valid_ipv4", fn_name="is_valid_ipv4",
         prompt="Write a Python function `is_valid_ipv4(s)` that returns True if "
                "s is a syntactically valid IPv4 address (4 dot-separated "
                "numbers, each 0-255, no extra characters), else False." + CODE_INSTR,
         tests=[(("192.168.1.1",), True), (("255.255.255.255",), True),
                (("256.1.1.1",), False), (("1.2.3",), False),
                (("1.2.3.4.5",), False), (("abc.1.1.1",), False)]),
    dict(id="parse_query_string", fn_name="parse_query_string",
         prompt="Write a Python function `parse_query_string(qs)` that parses "
                "a URL query string like 'a=1&b=hello&c=3' into a dict of "
                "string keys to string values." + CODE_INSTR,
         tests=[(("a=1&b=hello&c=3",), {"a": "1", "b": "hello", "c": "3"}),
                (("",), {}), (("x=1",), {"x": "1"})]),
    dict(id="parse_csv_line", fn_name="parse_csv_line",
         prompt="Write a Python function `parse_csv_line(line)` that splits a "
                "single CSV line into a list of fields, splitting on commas "
                "that are not inside double-quoted fields (quotes are removed "
                "from the output)." + CODE_INSTR,
         tests=[(('a,b,c',), ["a", "b", "c"]),
                (('a,"b,c",d',), ["a", "b,c", "d"]),
                (('"hello, world",42',), ["hello, world", "42"])]),
    dict(id="tokenize_simple_expr", fn_name="tokenize_simple_expr",
         prompt="Write a Python function `tokenize_simple_expr(expr)` that "
                "tokenizes a simple arithmetic expression string into a list "
                "of string tokens: integers, and the operators + - * / ( ) . "
                "Whitespace is ignored and not a token." + CODE_INSTR,
         tests=[(("12 + (3 * 4)",), ["12", "+", "(", "3", "*", "4", ")"]),
                (("1-2",), ["1", "-", "2"])]),
    dict(id="mask_credit_card", fn_name="mask_credit_card",
         prompt="Write a Python function `mask_credit_card(s)` that takes a "
                "16-digit credit card number as a string and returns it with "
                "all but the last 4 digits replaced by '*' (same length, no "
                "spaces added)." + CODE_INSTR,
         tests=[(("1234567812345678",), "************5678"),
                (("0000000000000001",), "************0001")]),
    dict(id="camel_to_snake", fn_name="camel_to_snake",
         prompt="Write a Python function `camel_to_snake(s)` that converts a "
                "CamelCase or camelCase string to snake_case." + CODE_INSTR,
         tests=[(("CamelCase",), "camel_case"), (("camelCase",), "camel_case"),
                (("HTTPServerError",), "http_server_error")]),
    dict(id="snake_to_camel", fn_name="snake_to_camel",
         prompt="Write a Python function `snake_to_camel(s)` that converts a "
                "snake_case string to camelCase (first letter lowercase)." + CODE_INSTR,
         tests=[(("snake_case",), "snakeCase"), (("already",), "already"),
                (("multi_word_example",), "multiWordExample")]),

    # ---------------- J. graph / tree algorithms ----------------
    dict(id="bfs_shortest_path", fn_name="bfs_shortest_path",
         prompt="Write a Python function `bfs_shortest_path(graph, start, end)` "
                "where graph is a dict mapping each node to a list of "
                "neighbors. Return a list of nodes forming a shortest path "
                "from start to end (inclusive), using breadth-first search." + CODE_INSTR,
         tests=[(({"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}, "A", "D"),
                  ["A", "B", "D"])]),
    dict(id="dfs_order", fn_name="dfs_order",
         prompt="Write a Python function `dfs_order(graph, start)` where graph "
                "is a dict mapping each node to a list of neighbors (in the "
                "order they should be visited). Return a list of nodes in the "
                "order visited by depth-first search starting at `start`, not "
                "revisiting nodes." + CODE_INSTR,
         tests=[(({"A": ["B", "C"], "B": ["D"], "C": [], "D": []}, "A"),
                  ["A", "B", "D", "C"])]),
    dict(id="has_cycle_directed", fn_name="has_cycle_directed",
         prompt="Write a Python function `has_cycle_directed(graph)` where "
                "graph is a dict mapping each node to a list of nodes it has a "
                "directed edge to. Return True if the graph contains a cycle, "
                "else False." + CODE_INSTR,
         tests=[(({"A": ["B"], "B": ["C"], "C": ["A"]},), True),
                (({"A": ["B"], "B": ["C"], "C": []},), False)]),
    dict(id="topological_sort",
         prompt="Write a Python function `topological_sort(graph)` where graph "
                "is a dict mapping each node (a DAG, no cycles) to a list of "
                "nodes it points to. Return a list of all nodes in a valid "
                "topological order (for every edge u -> v, u must appear "
                "before v)." + CODE_INSTR,
         custom_check=lambda ns: _check_topo_sort(ns)),
    dict(id="count_connected_components", fn_name="count_connected_components",
         prompt="Write a Python function `count_connected_components(graph)` "
                "where graph is a dict mapping each node to a list of its "
                "undirected neighbors. Return the number of connected "
                "components." + CODE_INSTR,
         tests=[(({"A": ["B"], "B": ["A"], "C": ["D"], "D": ["C"], "E": []},), 3)]),
    dict(id="tree_max_depth", fn_name="tree_max_depth",
         prompt="Write a Python function `tree_max_depth(tree)` where tree is "
                "a nested dict of the form {'value': v, 'children': [subtree, "
                "...]} (children may be an empty list for a leaf). Return the "
                "maximum depth (a single leaf node has depth 1)." + CODE_INSTR,
         tests=[(({"value": 1, "children": [
                    {"value": 2, "children": []},
                    {"value": 3, "children": [{"value": 4, "children": []}]},
                ]},), 3)]),
    dict(id="tree_sum", fn_name="tree_sum",
         prompt="Write a Python function `tree_sum(tree)` where tree is a "
                "nested dict of the form {'value': v, 'children': [subtree, "
                "...]}. Return the sum of all `value`s in the tree." + CODE_INSTR,
         tests=[(({"value": 1, "children": [
                    {"value": 2, "children": []},
                    {"value": 3, "children": [{"value": 4, "children": []}]},
                ]},), 10)]),
    dict(id="dijkstra_shortest_dist", fn_name="dijkstra_shortest_dist",
         prompt="Write a Python function `dijkstra_shortest_dist(graph, start)` "
                "where graph is a dict mapping each node to a list of (neighbor, "
                "weight) tuples (non-negative weights). Return a dict mapping "
                "every reachable node to its shortest distance from `start` "
                "(start maps to 0)." + CODE_INSTR,
         tests=[(({"A": [("B", 1), ("C", 4)], "B": [("C", 2)], "C": []}, "A"),
                  {"A": 0, "B": 1, "C": 3})]),

    # ---------------- K. exception handling & robustness ----------------
    dict(id="safe_divide", fn_name="safe_divide",
         prompt="Write a Python function `safe_divide(a, b)` that returns "
                "a / b, or None if b is 0 (never raises)." + CODE_INSTR,
         tests=[((10, 2), 5.0), ((5, 0), None)]),
    dict(id="parse_int_safe", fn_name="parse_int_safe",
         prompt="Write a Python function `parse_int_safe(s, default=0)` that "
                "returns int(s), or `default` if s can't be parsed as an "
                "integer (never raises)." + CODE_INSTR,
         tests=[(("42",), 42), (("abc",), 0), (("7", 99), 7), (("xx", 99), 99)]),
    dict(id="validate_age",
         prompt="Write a Python function `validate_age(age)` that returns age "
                "unchanged if it's an integer between 0 and 150 inclusive, else "
                "raises `ValueError`." + CODE_INSTR,
         custom_check=lambda ns: _check_validate_age(ns)),
    dict(id="insufficient_funds",
         prompt="Write a custom exception class `InsufficientFundsError(Exception)` "
                "and a function `withdraw(balance, amount)` that returns "
                "`balance - amount` if amount <= balance, else raises "
                "`InsufficientFundsError`." + CODE_INSTR,
         custom_check=lambda ns: _check_insufficient_funds(ns)),
    dict(id="timer_context_manager",
         prompt="Write a Python class `Timer` usable as a context manager "
                "(`with Timer() as t: ...`) that records the elapsed wall-clock "
                "time of the `with` block in seconds as a float attribute "
                "`t.elapsed`, available after the block exits." + CODE_INSTR,
         custom_check=lambda ns: _check_timer(ns)),
    dict(id="divide_all_custom_error",
         prompt="Write a custom exception class `DivisionError(Exception)` that "
                "stores the failing index as an attribute `index`, and a "
                "function `divide_all(nums, divisor)` that returns "
                "[n / divisor for n in nums], but if divisor is 0, raises "
                "`DivisionError` with `index` set to 0 (before doing any "
                "division) instead of letting ZeroDivisionError propagate." + CODE_INSTR,
         custom_check=lambda ns: _check_divide_all(ns)),

    # ---------------- L. async ----------------
    dict(id="async_double",
         prompt="Write an async Python function `async_double(x)` that returns "
                "x * 2 (it can simply be `async def` with no actual await "
                "needed, or use `await asyncio.sleep(0)`)." + CODE_INSTR,
         custom_check=lambda ns: _check_async_double(ns)),
    dict(id="async_gather_sum",
         prompt="Write an async Python function `async_gather_sum(nums)` that "
                "concurrently computes `n * 2` for each n in nums (using "
                "`asyncio.gather` with small async helper coroutines) and "
                "returns the sum of all the results." + CODE_INSTR,
         custom_check=lambda ns: _check_async_gather_sum(ns)),
    dict(id="async_countdown",
         prompt="Write an async Python function `async_countdown(n)` that "
                "returns a list counting down from n to 0 inclusive (n, n-1, "
                "..., 1, 0), awaiting `asyncio.sleep(0)` between steps." + CODE_INSTR,
         custom_check=lambda ns: _check_async_countdown(ns)),

    # ---------------- M. misc advanced ----------------
    dict(id="levenshtein_distance", fn_name="levenshtein_distance",
         prompt="Write a Python function `levenshtein_distance(a, b)` that "
                "returns the edit distance (minimum insertions/deletions/"
                "substitutions) between strings a and b." + CODE_INSTR,
         tests=[(("kitten", "sitting"), 3), (("", "abc"), 3), (("abc", "abc"), 0)]),
    dict(id="matrix_is_symmetric", fn_name="matrix_is_symmetric",
         prompt="Write a Python function `matrix_is_symmetric(matrix)` that "
                "returns True if the square 2D list `matrix` is symmetric "
                "(matrix[i][j] == matrix[j][i] for all i, j), else False." + CODE_INSTR,
         tests=[(([[1,2,3],[2,4,5],[3,5,6]],), True),
                (([[1,2],[3,4]],), False)]),
    dict(id="rle_decode", fn_name="rle_decode",
         prompt="Write a Python function `rle_decode(s)` that decodes a "
                "run-length-encoded string of the form 'a3b2c1' (character "
                "followed by its count) back into the original string, e.g. "
                "'a3b2c1' -> 'aaabbc'." + CODE_INSTR,
         tests=[(("a3b2c1",), "aaabbc"), (("a1b1c1",), "abc"), (("",), "")]),
    dict(id="int_to_base", fn_name="int_to_base",
         prompt="Write a Python function `int_to_base(n, base)` that converts "
                "non-negative integer n to its string representation in the "
                "given base (2 to 16 inclusive), using digits 0-9 then a-f for "
                "10-15. Do not use Python's built-in format specifiers for "
                "arbitrary bases (bin/hex/oct/format-with-base are fine to "
                "avoid, but must work for any base 2-16)." + CODE_INSTR,
         tests=[((10, 2), "1010"), ((255, 16), "ff"), ((0, 2), "0"), ((8, 8), "10")]),
    dict(id="longest_palindromic_substring",
         prompt="Write a Python function `longest_palindromic_substring(s)` "
                "that returns the longest palindromic substring of s. If there "
                "are multiple of the same maximum length, return any one of "
                "them." + CODE_INSTR,
         custom_check=lambda ns: _check_longest_palindrome(ns)),
    dict(id="rotate_matrix_90", fn_name="rotate_matrix_90",
         prompt="Write a Python function `rotate_matrix_90(matrix)` that "
                "returns a NEW NxN matrix (2D list) rotated 90 degrees "
                "clockwise from the input `matrix`." + CODE_INSTR,
         tests=[(([[1,2],[3,4]],), [[3,1],[4,2]]),
                (([[1,2,3],[4,5,6],[7,8,9]],), [[7,4,1],[8,5,2],[9,6,3]])]),
    dict(id="flatten_dict", fn_name="flatten_dict",
         prompt="Write a Python function `flatten_dict(d, sep='.')` that "
                "flattens an arbitrarily nested dict into a single-level dict "
                "whose keys are the joined path of nested keys using `sep`, "
                "e.g. {'a': {'b': 1, 'c': {'d': 2}}} -> {'a.b': 1, 'a.c.d': 2}." + CODE_INSTR,
         tests=[(({"a": {"b": 1, "c": {"d": 2}}, "e": 3},),
                  {"a.b": 1, "a.c.d": 2, "e": 3})]),
]


# ---------------------------------------------------------------------------
# custom_check helpers (kept out of the lambda soup above for the trickier
# problems: decorators, exceptions, async, and ties-allowed answers)
# ---------------------------------------------------------------------------
def _run_bank_check(ns) -> tuple[bool, str]:
    a = ns["BankAccount"](100)
    a.deposit(50)
    if a.balance != 150:
        return _fail(f"after deposit(50) on balance=100, expected 150, got {a.balance}")
    a.withdraw(30)
    if a.balance != 120:
        return _fail(f"after withdraw(30), expected 120, got {a.balance}")
    try:
        a.withdraw(1000)
        return _fail("withdraw(1000) on balance=120 should have raised ValueError")
    except ValueError:
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _fail(f"withdraw over balance raised {type(e).__name__}, expected ValueError")


def _check_memoize(ns) -> tuple[bool, str]:
    calls = {"n": 0}

    @ns["memoize"]
    def slow_square(x):
        calls["n"] += 1
        return x * x

    r1 = slow_square(5)
    r2 = slow_square(5)
    r3 = slow_square(6)
    if (r1, r2, r3) != (25, 25, 36):
        return _fail(f"memoized results wrong: got {(r1, r2, r3)!r}, expected (25, 25, 36)")
    if calls["n"] != 2:
        return _fail(f"underlying function called {calls['n']} times for "
                      f"(5, 5, 6) — expected exactly 2 (5 cached on 2nd call)")
    return _ok()


def _check_retry(ns) -> tuple[bool, str]:
    state = {"n": 0}

    @ns["retry"](3)
    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise ValueError("not yet")
        return "success"

    result = flaky()
    if result != "success":
        return _fail(f"retry(3)-wrapped function returned {result!r}, expected 'success'")
    if state["n"] != 3:
        return _fail(f"expected exactly 3 attempts, function ran {state['n']} times")
    return _ok()


def _check_once(ns) -> tuple[bool, str]:
    calls = {"n": 0}

    @ns["once"]
    def side_effecting():
        calls["n"] += 1
        return calls["n"]

    r1 = side_effecting()
    r2 = side_effecting()
    r3 = side_effecting()
    if not (r1 == r2 == r3 == 1):
        return _fail(f"once()-wrapped calls returned {(r1, r2, r3)!r}, "
                      f"expected (1, 1, 1) — body must run only once")
    if calls["n"] != 1:
        return _fail(f"underlying function body ran {calls['n']} times, expected 1")
    return _ok()


def _check_topo_sort(ns) -> tuple[bool, str]:
    graph = {"A": ["C"], "B": ["C", "D"], "C": ["E"], "D": ["E"], "E": []}
    order = ns["topological_sort"](graph)
    if sorted(order) != sorted(graph.keys()):
        return _fail(f"topological_sort returned {order!r}, "
                      f"not a permutation of {sorted(graph.keys())!r}")
    pos = {node: i for i, node in enumerate(order)}
    for u, neighbors in graph.items():
        for v in neighbors:
            if pos[u] > pos[v]:
                return _fail(f"invalid order {order!r}: edge {u}->{v} violated "
                              f"({u} at {pos[u]}, {v} at {pos[v]})")
    return _ok()


def _check_validate_age(ns) -> tuple[bool, str]:
    fn = ns["validate_age"]
    if fn(30) != 30:
        return _fail(f"validate_age(30) = {fn(30)!r}, expected 30")
    if fn(0) != 0:
        return _fail(f"validate_age(0) = {fn(0)!r}, expected 0")
    try:
        fn(-1)
        return _fail("validate_age(-1) should have raised ValueError")
    except ValueError:
        pass
    except Exception as e:  # noqa: BLE001
        return _fail(f"validate_age(-1) raised {type(e).__name__}, expected ValueError")
    try:
        fn(200)
        return _fail("validate_age(200) should have raised ValueError")
    except ValueError:
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _fail(f"validate_age(200) raised {type(e).__name__}, expected ValueError")


def _check_insufficient_funds(ns) -> tuple[bool, str]:
    withdraw = ns["withdraw"]
    Err = ns["InsufficientFundsError"]
    if not (isinstance(Err, type) and issubclass(Err, Exception)):
        return _fail("InsufficientFundsError is not an Exception subclass")
    r = withdraw(100, 40)
    if r != 60:
        return _fail(f"withdraw(100, 40) = {r!r}, expected 60")
    try:
        withdraw(50, 100)
        return _fail("withdraw(50, 100) should have raised InsufficientFundsError")
    except Err:
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _fail(f"withdraw(50, 100) raised {type(e).__name__}, "
                      f"expected InsufficientFundsError")


def _check_timer(ns) -> tuple[bool, str]:
    import time
    with ns["Timer"]() as t:
        time.sleep(0.05)
    if not hasattr(t, "elapsed"):
        return _fail("Timer instance has no `elapsed` attribute after the with block")
    if not isinstance(t.elapsed, (int, float)) or t.elapsed < 0.03:
        return _fail(f"t.elapsed = {t.elapsed!r}, expected a float >= ~0.05 "
                      f"(the sleep duration)")
    return _ok()


def _check_divide_all(ns) -> tuple[bool, str]:
    divide_all = ns["divide_all"]
    Err = ns["DivisionError"]
    r = divide_all([10, 20, 30], 2)
    if r != [5.0, 10.0, 15.0]:
        return _fail(f"divide_all([10,20,30], 2) = {r!r}, expected [5.0, 10.0, 15.0]")
    try:
        divide_all([1, 2, 3], 0)
        return _fail("divide_all([1,2,3], 0) should have raised DivisionError")
    except Err as e:
        if getattr(e, "index", None) != 0:
            return _fail(f"DivisionError.index = {getattr(e, 'index', None)!r}, expected 0")
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _fail(f"divide_all with divisor=0 raised {type(e).__name__}, "
                      f"expected DivisionError")


def _check_async_double(ns) -> tuple[bool, str]:
    r = _run_coro(ns["async_double"](21))
    if r != 42:
        return _fail(f"async_double(21) = {r!r}, expected 42")
    return _ok()


def _check_async_gather_sum(ns) -> tuple[bool, str]:
    r = _run_coro(ns["async_gather_sum"]([1, 2, 3, 4]))
    if r != 20:
        return _fail(f"async_gather_sum([1,2,3,4]) = {r!r}, expected 20 "
                      f"(sum of [2,4,6,8])")
    return _ok()


def _check_async_countdown(ns) -> tuple[bool, str]:
    r = _run_coro(ns["async_countdown"](3))
    if r != [3, 2, 1, 0]:
        return _fail(f"async_countdown(3) = {r!r}, expected [3, 2, 1, 0]")
    return _ok()


def _is_palindrome_str(s: str) -> bool:
    return s == s[::-1]


def _check_longest_palindrome(ns) -> tuple[bool, str]:
    fn = ns["longest_palindromic_substring"]
    r = fn("babad")
    if r not in ("bab", "aba"):
        if not (isinstance(r, str) and len(r) == 3 and _is_palindrome_str(r) and r in "babad"):
            return _fail(f"longest_palindromic_substring('babad') = {r!r}, "
                          f"expected a length-3 palindromic substring ('bab' or 'aba')")
    r2 = fn("cbbd")
    if r2 != "bb":
        return _fail(f"longest_palindromic_substring('cbbd') = {r2!r}, expected 'bb'")
    return _ok()


assert len(PROBLEMS) == 50, f"expected 50 advanced problems, got {len(PROBLEMS)}"
