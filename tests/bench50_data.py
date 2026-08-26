"""50 self-contained, auto-gradable Python coding problems for the
gpt-oss-20b vs Qwen3.6-27B-3bit-mlx benchmark (see tests/bench50_run.py).

Each problem is graded by exec-ing the model's extracted function and
calling it with fixed inputs, comparing against a known-correct expected
value. No custom classes (linked lists / trees) are used so grading never
depends on the model matching an exact class shape — only a plain function
over ints/strs/lists/dicts/tuples/bools.

Each entry:
  id       - short slug
  fn_name  - exact function name the prompt demands
  prompt   - full instruction sent to the model
  tests    - list of (args_tuple, expected) for plain equality checks
  cmp      - optional custom comparator(actual, expected) -> bool, for
             problems where output order/format isn't unique (e.g. set-like
             results). Defaults to `==`.
"""
from __future__ import annotations

CODE_INSTR = (
    " Return ONLY a single Python code block containing just the function "
    "definition (plus any small helper it needs) — no explanation, no example "
    "usage, no prose before or after."
)


def _sorted_groups(actual, expected) -> bool:
    try:
        a = sorted(sorted(g) for g in actual)
        e = sorted(sorted(g) for g in expected)
        return a == e
    except TypeError:
        return False


def _set_of_frozensets(actual, expected) -> bool:
    try:
        return {frozenset(s) for s in actual} == {frozenset(s) for s in expected}
    except TypeError:
        return False


PROBLEMS: list[dict] = [
    # ---------------- A. basics ----------------
    dict(id="fizzbuzz_list", fn_name="fizzbuzz_list",
         prompt="Write a Python function `fizzbuzz_list(n)` that returns a list of "
                "strings for 1..n inclusive: 'Fizz' if divisible by 3, 'Buzz' if by 5, "
                "'FizzBuzz' if both, else the number as a string." + CODE_INSTR,
         tests=[((15,), ["1","2","Fizz","4","Buzz","Fizz","7","8","Fizz","Buzz",
                          "11","Fizz","13","14","FizzBuzz"]),
                ((1,), ["1"])]),
    dict(id="is_prime", fn_name="is_prime",
         prompt="Write a Python function `is_prime(n)` that returns True if n is a "
                "prime number, else False. Handle n <= 1 correctly." + CODE_INSTR,
         tests=[((2,), True), ((1,), False), ((0,), False), ((17,), True),
                ((18,), False), ((97,), True)]),
    dict(id="factorial", fn_name="factorial",
         prompt="Write a Python function `factorial(n)` that returns n! for n >= 0 "
                "(factorial(0) == 1)." + CODE_INSTR,
         tests=[((0,), 1), ((1,), 1), ((5,), 120), ((10,), 3628800)]),
    dict(id="gcd_fn", fn_name="gcd_fn",
         prompt="Write a Python function `gcd_fn(a, b)` that returns the greatest "
                "common divisor of two positive integers a and b." + CODE_INSTR,
         tests=[((48, 18), 6), ((17, 13), 1), ((100, 25), 25)]),
    dict(id="lcm_fn", fn_name="lcm_fn",
         prompt="Write a Python function `lcm_fn(a, b)` that returns the least "
                "common multiple of two positive integers a and b." + CODE_INSTR,
         tests=[((4, 6), 12), ((21, 6), 42), ((7, 5), 35)]),
    dict(id="reverse_string", fn_name="reverse_string",
         prompt="Write a Python function `reverse_string(s)` that returns the "
                "string s reversed." + CODE_INSTR,
         tests=[(("hello",), "olleh"), (("",), ""), (("a",), "a")]),
    dict(id="is_palindrome", fn_name="is_palindrome",
         prompt="Write a Python function `is_palindrome(s)` that returns True if s "
                "is a palindrome, ignoring case, spaces, and punctuation (consider "
                "only alphanumeric characters). Else False." + CODE_INSTR,
         tests=[(("Was it a car or a cat I saw?",), True),
                (("hello",), False), (("",), True), (("Madam",), True)]),
    dict(id="sum_digits", fn_name="sum_digits",
         prompt="Write a Python function `sum_digits(n)` that returns the sum of "
                "the digits of a non-negative integer n." + CODE_INSTR,
         tests=[((0,), 0), ((123,), 6), ((9999,), 36)]),
    dict(id="count_vowels", fn_name="count_vowels",
         prompt="Write a Python function `count_vowels(s)` that returns the number "
                "of vowels (a,e,i,o,u, case-insensitive) in string s." + CODE_INSTR,
         tests=[(("Hello World",), 3), (("xyz",), 0), (("AEIOUaeiou",), 10)]),
    dict(id="is_leap_year", fn_name="is_leap_year",
         prompt="Write a Python function `is_leap_year(y)` that returns True if y "
                "is a leap year per the Gregorian calendar rules." + CODE_INSTR,
         tests=[((2000,), True), ((1900,), False), ((2024,), True), ((2023,), False)]),

    # ---------------- B. lists/arrays ----------------
    dict(id="two_sum", fn_name="two_sum",
         prompt="Write a Python function `two_sum(nums, target)` that returns a "
                "tuple (i, j) with i < j of the indices of the two numbers in nums "
                "that add up to target. Assume exactly one solution exists." + CODE_INSTR,
         tests=[(([2, 7, 11, 15], 9), (0, 1)), (([3, 2, 4], 6), (1, 2)),
                (([3, 3], 6), (0, 1))]),
    dict(id="max_subarray", fn_name="max_subarray",
         prompt="Write a Python function `max_subarray(nums)` that returns the "
                "largest sum of any contiguous subarray of nums (Kadane's "
                "algorithm). nums has at least one element." + CODE_INSTR,
         tests=[(([-2,1,-3,4,-1,2,1,-5,4],), 6), (([1],), 1), (([5,4,-1,7,8],), 23),
                (([-1,-2,-3],), -1)]),
    dict(id="rotate_array", fn_name="rotate_array",
         prompt="Write a Python function `rotate_array(nums, k)` that returns a "
                "new list with nums rotated to the right by k steps (k can be >= "
                "len(nums))." + CODE_INSTR,
         tests=[(([1,2,3,4,5,6,7], 3), [5,6,7,1,2,3,4]),
                (([1,2,3], 4), [3,1,2]), (([1,2], 0), [1,2])]),
    dict(id="dedupe_preserve_order", fn_name="dedupe_preserve_order",
         prompt="Write a Python function `dedupe_preserve_order(nums)` that returns "
                "a list with duplicates removed, keeping only the first occurrence "
                "of each value, preserving original order." + CODE_INSTR,
         tests=[(([1,2,1,3,2,4],), [1,2,3,4]), (([],), []), (([1,1,1],), [1])]),
    dict(id="flatten_list", fn_name="flatten_list",
         prompt="Write a Python function `flatten_list(nested)` that flattens an "
                "arbitrarily nested list of integers into a single flat list, "
                "preserving order." + CODE_INSTR,
         tests=[(([1, [2, 3, [4, 5]], 6],), [1,2,3,4,5,6]),
                (([[1,[2,[3,[4]]]]],), [1,2,3,4]), (([],), [])]),
    dict(id="chunk_list", fn_name="chunk_list",
         prompt="Write a Python function `chunk_list(lst, size)` that splits lst "
                "into a list of chunks (lists), each of length `size` except "
                "possibly the last one which may be shorter." + CODE_INSTR,
         tests=[(([1,2,3,4,5], 2), [[1,2],[3,4],[5]]),
                (([1,2,3], 5), [[1,2,3]]), (([], 3), [])]),
    dict(id="moving_average", fn_name="moving_average",
         prompt="Write a Python function `moving_average(nums, window)` that "
                "returns a list of the moving averages of nums with the given "
                "window size (only full windows, as floats)." + CODE_INSTR,
         tests=[(([1,2,3,4,5], 2), [1.5,2.5,3.5,4.5]),
                (([1,2,3,4,5], 3), [2.0,3.0,4.0])]),
    dict(id="missing_number", fn_name="missing_number",
         prompt="Write a Python function `missing_number(nums)` where nums "
                "contains n distinct numbers from the range 0..n (inclusive) with "
                "exactly one missing. Return the missing number." + CODE_INSTR,
         tests=[(([3,0,1],), 2), (([0,1],), 2), (([9,6,4,2,3,5,7,0,1],), 8)]),
    dict(id="single_number_xor", fn_name="single_number_xor",
         prompt="Write a Python function `single_number_xor(nums)` where every "
                "element appears exactly twice except for one which appears once. "
                "Return that single element." + CODE_INSTR,
         tests=[(([2,2,1],), 1), (([4,1,2,1,2],), 4), (([1],), 1)]),
    dict(id="intersection_two_arrays", fn_name="intersection_two_arrays",
         prompt="Write a Python function `intersection_two_arrays(a, b)` that "
                "returns a sorted list of the unique values present in both lists "
                "a and b." + CODE_INSTR,
         tests=[(([1,2,2,1], [2,2]), [2]), (([4,9,5], [9,4,9,8,4]), [4,9]),
                (([1,2], [3,4]), [])]),

    # ---------------- C. strings ----------------
    dict(id="valid_parentheses", fn_name="valid_parentheses",
         prompt="Write a Python function `valid_parentheses(s)` that returns True "
                "if the brackets in s — types (), [], {} — are all properly "
                "matched and nested, else False. s may contain other characters "
                "which should be ignored." + CODE_INSTR,
         tests=[(("()[]{}",), True), (("(]",), False), (("([{}])",), True),
                (("(a[b]{c}d",), False), (("",), True)]),
    dict(id="longest_common_prefix", fn_name="longest_common_prefix",
         prompt="Write a Python function `longest_common_prefix(strs)` that "
                "returns the longest common prefix string among a list of strings. "
                "Return '' if there is none or the list is empty." + CODE_INSTR,
         tests=[((["flower","flow","flight"],), "fl"),
                ((["dog","racecar","car"],), ""), (([],), "")]),
    dict(id="run_length_encode", fn_name="run_length_encode",
         prompt="Write a Python function `run_length_encode(s)` that run-length "
                "encodes s, e.g. 'aaabbc' -> 'a3b2c1'. Each run is written as the "
                "character followed by its count (even count 1)." + CODE_INSTR,
         tests=[(("aaabbc",), "a3b2c1"), (("abc",), "a1b1c1"), (("",), "")]),
    dict(id="caesar_cipher", fn_name="caesar_cipher",
         prompt="Write a Python function `caesar_cipher(s, shift)` that shifts "
                "each letter in s by `shift` positions (wrapping a-z and A-Z "
                "separately), leaving non-letters unchanged." + CODE_INSTR,
         tests=[(("abc", 1), "bcd"), (("xyz", 2), "zab"),
                (("Hello, World!", 3), "Khoor, Zruog!")]),
    dict(id="is_anagram", fn_name="is_anagram",
         prompt="Write a Python function `is_anagram(a, b)` that returns True if "
                "strings a and b are anagrams of each other (same letters, same "
                "counts, case-insensitive, ignoring spaces), else False." + CODE_INSTR,
         tests=[(("listen", "silent"), True), (("hello", "world"), False),
                (("Dormitory", "Dirty Room"), True)]),
    dict(id="string_compression", fn_name="string_compression",
         prompt="Write a Python function `string_compression(s)` that compresses "
                "s using counts of repeated characters, e.g. 'aabcccccaaa' -> "
                "'a2b1c5a3'. Always returns the compressed form even if longer." + CODE_INSTR,
         tests=[(("aabcccccaaa",), "a2b1c5a3"), (("abcd",), "a1b1c1d1")]),
    dict(id="word_frequency", fn_name="word_frequency",
         prompt="Write a Python function `word_frequency(s)` that returns a dict "
                "mapping each lowercase word (split on whitespace, punctuation "
                "stripped) to its count in s." + CODE_INSTR,
         tests=[(("the cat sat on the mat",), {"the":2,"cat":1,"sat":1,"on":1,"mat":1})]),
    dict(id="reverse_words_in_sentence", fn_name="reverse_words_in_sentence",
         prompt="Write a Python function `reverse_words_in_sentence(s)` that "
                "reverses the order of words in s (single-space separated in the "
                "output), trimming extra whitespace." + CODE_INSTR,
         tests=[(("the sky is blue",), "blue is sky the"),
                (("  hello   world  ",), "world hello")]),
    dict(id="first_unique_char", fn_name="first_unique_char",
         prompt="Write a Python function `first_unique_char(s)` that returns the "
                "index of the first non-repeating character in s, or -1 if none." + CODE_INSTR,
         tests=[(("leetcode",), 0), (("loveleetcode",), 2), (("aabb",), -1)]),
    dict(id="roman_to_int", fn_name="roman_to_int",
         prompt="Write a Python function `roman_to_int(s)` that converts a Roman "
                "numeral string s to its integer value." + CODE_INSTR,
         tests=[(("III",), 3), (("LVIII",), 58), (("MCMXCIV",), 1994), (("IX",), 9)]),

    # ---------------- D. sorting/searching ----------------
    dict(id="binary_search", fn_name="binary_search",
         prompt="Write a Python function `binary_search(arr, target)` that "
                "returns the index of target in the sorted list arr, or -1 if "
                "not present. Must run in O(log n)." + CODE_INSTR,
         tests=[(([1,3,5,7,9,11], 7), 3), (([1,3,5,7,9,11], 4), -1),
                (([], 1), -1)]),
    dict(id="quicksort", fn_name="quicksort",
         prompt="Write a Python function `quicksort(arr)` that returns a new "
                "list with the elements of arr sorted ascending, implemented via "
                "the quicksort algorithm." + CODE_INSTR,
         tests=[(([3,6,8,10,1,2,1],), [1,1,2,3,6,8,10]), (([],), []),
                (([5,4,3,2,1],), [1,2,3,4,5])]),
    dict(id="mergesort", fn_name="mergesort",
         prompt="Write a Python function `mergesort(arr)` that returns a new "
                "list with the elements of arr sorted ascending, implemented via "
                "the merge sort algorithm." + CODE_INSTR,
         tests=[(([5,2,4,6,1,3],), [1,2,3,4,5,6]), (([1],), [1]), (([],), [])]),
    dict(id="kth_largest", fn_name="kth_largest",
         prompt="Write a Python function `kth_largest(nums, k)` that returns the "
                "k-th largest element in nums (k=1 means the largest)." + CODE_INSTR,
         tests=[(([3,2,1,5,6,4], 2), 5), (([3,2,3,1,2,4,5,5,6], 4), 4)]),
    dict(id="merge_intervals", fn_name="merge_intervals",
         prompt="Write a Python function `merge_intervals(intervals)` that merges "
                "overlapping intervals given as a list of [start, end] pairs and "
                "returns the merged list sorted by start." + CODE_INSTR,
         tests=[(([[1,3],[2,6],[8,10],[15,18]],), [[1,6],[8,10],[15,18]]),
                (([[1,4],[4,5]],), [[1,5]])]),

    # ---------------- E. recursion/counting ----------------
    dict(id="power_set", fn_name="power_set",
         prompt="Write a Python function `power_set(lst)` that returns a list of "
                "all subsets of lst (each subset a list), including the empty set "
                "and lst itself. Order of subsets/elements does not matter." + CODE_INSTR,
         tests=[(([1,2],), [[],[1],[2],[1,2]])], cmp="frozensets"),
    dict(id="count_permutations", fn_name="count_permutations",
         prompt="Write a Python function `count_permutations(n, r)` that returns "
                "nPr, the number of ways to arrange r items out of n (order "
                "matters)." + CODE_INSTR,
         tests=[((5, 2), 20), ((4, 4), 24), ((6, 0), 1)]),
    dict(id="count_combinations", fn_name="count_combinations",
         prompt="Write a Python function `count_combinations(n, r)` that returns "
                "nCr, the number of ways to choose r items out of n (order does "
                "not matter)." + CODE_INSTR,
         tests=[((5, 2), 10), ((4, 4), 1), ((6, 0), 1)]),
    dict(id="coin_change_min", fn_name="coin_change_min",
         prompt="Write a Python function `coin_change_min(coins, amount)` that "
                "returns the minimum number of coins from `coins` needed to make "
                "up `amount`, or -1 if it can't be made." + CODE_INSTR,
         tests=[(([1,2,5], 11), 3), (([2], 3), -1), (([1], 0), 0)]),
    dict(id="longest_increasing_subsequence_length", fn_name="longest_increasing_subsequence_length",
         prompt="Write a Python function `longest_increasing_subsequence_length(nums)` "
                "that returns the length of the longest strictly increasing "
                "subsequence of nums." + CODE_INSTR,
         tests=[(([10,9,2,5,3,7,101,18],), 4), (([0,1,0,3,2,3],), 4), (([7,7,7],), 1)]),
    dict(id="fibonacci_memo", fn_name="fibonacci_memo",
         prompt="Write a Python function `fibonacci_memo(n)` that returns the "
                "n-th Fibonacci number (fibonacci_memo(0)==0, fibonacci_memo(1)==1), "
                "efficiently (memoized/iterative, not naive exponential recursion)." + CODE_INSTR,
         tests=[((0,), 0), ((1,), 1), ((10,), 55), ((30,), 832040)]),
    dict(id="matrix_transpose", fn_name="matrix_transpose",
         prompt="Write a Python function `matrix_transpose(matrix)` that returns "
                "the transpose of a 2D list `matrix`." + CODE_INSTR,
         tests=[(([[1,2,3],[4,5,6]],), [[1,4],[2,5],[3,6]]),
                (([[1]],), [[1]])]),
    dict(id="celsius_to_fahrenheit", fn_name="celsius_to_fahrenheit",
         prompt="Write a Python function `celsius_to_fahrenheit(c)` that converts "
                "a temperature in Celsius to Fahrenheit (return a float)." + CODE_INSTR,
         tests=[((0,), 32.0), ((100,), 212.0), ((-40,), -40.0)]),
    dict(id="group_anagrams", fn_name="group_anagrams",
         prompt="Write a Python function `group_anagrams(words)` that groups "
                "words that are anagrams of each other, returning a list of "
                "groups (each a list of the original words). Order of groups and "
                "words within a group does not matter." + CODE_INSTR,
         tests=[((["eat","tea","tan","ate","nat","bat"],),
                  [["eat","tea","ate"],["tan","nat"],["bat"]])],
         cmp="groups"),
    dict(id="majority_element", fn_name="majority_element",
         prompt="Write a Python function `majority_element(nums)` that returns "
                "the element appearing more than n/2 times in nums (guaranteed to "
                "exist)." + CODE_INSTR,
         tests=[(([3,2,3],), 3), (([2,2,1,1,1,2,2],), 2)]),

    # ---------------- F. misc ----------------
    dict(id="second_largest_unique", fn_name="second_largest_unique",
         prompt="Write a Python function `second_largest_unique(nums)` that "
                "returns the second-largest UNIQUE number in nums, or None if "
                "there isn't one (e.g. all elements equal, or fewer than 2 "
                "elements)." + CODE_INSTR,
         tests=[(([2,2,2],), None), (([1,2,2,3],), 2), (([5],), None),
                (([4,1,2],), 2)]),
    dict(id="matrix_multiply", fn_name="matrix_multiply",
         prompt="Write a Python function `matrix_multiply(a, b)` that returns the "
                "matrix product of 2D lists a and b (standard matrix "
                "multiplication)." + CODE_INSTR,
         tests=[(([[1,2],[3,4]], [[5,6],[7,8]]), [[19,22],[43,50]]),
                (([[1,2,3]], [[1],[2],[3]]), [[14]])]),
    dict(id="title_case", fn_name="title_case",
         prompt="Write a Python function `title_case(s)` that capitalizes the "
                "first letter of every word in s and lowercases the rest, words "
                "separated by single spaces in the output." + CODE_INSTR,
         tests=[(("hello world",), "Hello World"), (("THE QUICK FOX",), "The Quick Fox")]),
    dict(id="count_set_bits", fn_name="count_set_bits",
         prompt="Write a Python function `count_set_bits(n)` that returns the "
                "number of 1 bits in the binary representation of non-negative "
                "integer n." + CODE_INSTR,
         tests=[((0,), 0), ((7,), 3), ((255,), 8), ((1024,), 1)]),
    dict(id="gcd_list", fn_name="gcd_list",
         prompt="Write a Python function `gcd_list(nums)` that returns the "
                "greatest common divisor of a list of positive integers (at "
                "least one element)." + CODE_INSTR,
         tests=[(([12, 18, 24],), 6), (([7],), 7), (([5, 10, 15, 25],), 5)]),
]

assert len(PROBLEMS) == 50, f"expected 50 problems, got {len(PROBLEMS)}"


def check(problem: dict, fn, ns: dict | None = None) -> tuple[bool, str]:
    """Run all test cases for `problem` against callable `fn`. Returns
    (passed, error_message).

    If `problem` has a `custom_check` key instead of plain `tests` (used for
    OOP/generator/decorator problems where a single function call isn't
    enough to exercise behavior), it's called as `custom_check(ns)` with the
    full exec namespace and must return (passed, error_message) itself.
    """
    if "custom_check" in problem:
        try:
            return problem["custom_check"](ns if ns is not None else {})
        except Exception as e:  # noqa: BLE001
            return False, f"custom_check raised {type(e).__name__}: {e}"
    cmp = problem.get("cmp")
    for args, expected in problem["tests"]:
        try:
            actual = fn(*args)
        except Exception as e:  # noqa: BLE001
            return False, f"raised {type(e).__name__}: {e} on args={args!r}"
        if cmp == "frozensets":
            ok = _set_of_frozensets(actual, expected)
        elif cmp == "groups":
            ok = _sorted_groups(actual, expected)
        else:
            ok = actual == expected
        if not ok:
            return False, f"args={args!r} expected={expected!r} got={actual!r}"
    return True, ""
