"""V3.1 chat-code families (category 'chatcode'): requests that are plain text answers (write a snippet, a joke, a
definition, a short explanation) but sit next to tempting tools (run_shell, write_file, web_search, define_word).
Every target is a single direct final answer with no tool call. Prompts are original, not eval wording."""
import re

CAT = "chatcode"
FAIL = ("chat_as_tool",)

_NAMES = ("family", "messy")


def register(G):
    g = {n: getattr(G, n) for n in _NAMES}
    globals().update(g)

    USED = {}

    def qa(r, c, now, bank):
        # draw without replacement within a family/split so a small quota never repeats a template
        used = USED.setdefault((id(bank), G.CUR["split"]), set())
        free = [k for k in range(len(bank)) if k not in used]
        if not free:
            used.clear()
            free = list(range(len(bank)))
        i = r.choice(free)
        used.add(i)
        q, a = bank[i]
        assert len(a) < 900
        c.user(messy(r, q, protect=tuple(re.findall(r"`[^`]*`", q))))
        c.final(a)
        return {"template": i, "slots": {}}

    def reg(fid, bank, weight, split="train", web=False):
        @family(CAT, fid, split=split, weight=weight, failure=FAIL, web=web)
        def fn(r, c, now):
            return qa(r, c, now, bank)
        fn.__name__ = "f_" + fid
        return fn

    # ------------------------------------------------------------------ 1. code inline
    CODE = [
        ("can you write me a python function that removes duplicates from a list but keeps the original order",
         "```python\ndef dedupe(items):\n    seen = set()\n    return [x for x in items if not (x in seen or seen.add(x))]\n```\nIt walks the list once and keeps only the first time each value appears."),
        ("python function for the nth fibonacci number please",
         "```python\ndef fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n```\nThis is iterative, so it stays fast for large n; fib(10) is 55."),
        ("write a python function that says whether a year is a leap year",
         "```python\ndef is_leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n```\nDivisible by 4, except century years, which must also be divisible by 400."),
        ("give me a python one-liner that adds up the squares from 1 to 10",
         "```python\nprint(sum(i * i for i in range(1, 11)))\n```\nThat prints 385."),
        ("python function that merges two dictionaries, with the second one winning on clashes",
         "```python\ndef merge(a, b):\n    return {**a, **b}\n```\nKeys from b overwrite matching keys from a, and neither input is changed."),
        ("how do I load a json file into a dict in python",
         "```python\nimport json\n\nwith open(\"data.json\") as f:\n    data = json.load(f)\n```\nThe `with` block closes the file for you, and `data` is then a normal dict or list."),
        ("write a python function to find the most common word in a piece of text",
         "```python\nfrom collections import Counter\n\ndef top_word(text):\n    words = text.lower().split()\n    return Counter(words).most_common(1)[0][0]\n```\nIt lowercases the text, counts each word, and returns the most frequent one."),
        ("python function to convert celsius to fahrenheit",
         "```python\ndef c_to_f(c):\n    return c * 9 / 5 + 32\n```\nFor example, c_to_f(100) gives 212.0."),
        ("list comprehension in python that keeps only the odd numbers from nums",
         "```python\nodds = [n for n in nums if n % 2 == 1]\n```\nThe condition at the end filters out every even number."),
        ("write a javascript function that waits until the user stops typing before running something, a debounce",
         "```js\nfunction debounce(fn, delay) {\n  let timer;\n  return (...args) => {\n    clearTimeout(timer);\n    timer = setTimeout(() => fn(...args), delay);\n  };\n}\n```\nEach call resets the timer, so `fn` only runs after `delay` ms of quiet."),
        ("js one-liner to get the unique values out of an array",
         "```js\nconst unique = [...new Set(arr)];\n```\nA Set only keeps one copy of each value, and the spread turns it back into an array."),
        ("javascript function that capitalizes the first letter of every word in a sentence",
         "```js\nconst titleCase = (s) => s.replace(/\\b\\w/g, (ch) => ch.toUpperCase());\n```\nThe regex matches the first letter of each word and upper-cases it."),
        ("how do I sort an array of objects by an age field in javascript",
         "```js\npeople.sort((a, b) => a.age - b.age);\n```\nThis sorts in place from youngest to oldest; swap a and b for the reverse."),
        ("write a js function that sums an array of numbers",
         "```js\nconst sum = (nums) => nums.reduce((total, n) => total + n, 0);\n```\nStarting at 0 means an empty array safely returns 0."),
        ("bash one-liner to find the five biggest files under the current folder",
         "```bash\nfind . -type f -exec du -h {} + | sort -rh | head -n 5\n```\nIt lists every file with its size, sorts largest first, and keeps the top five."),
        ("shell loop to rename every .jpeg file in a folder to .jpg",
         "```bash\nfor f in *.jpeg; do mv \"$f\" \"${f%.jpeg}.jpg\"; done\n```\nThe `${f%.jpeg}` part strips the old extension so the new one can be added."),
        ("what's the command to see how much space each subfolder is using",
         "```bash\ndu -sh */\n```\nThis prints a human-readable total for every folder in the current directory."),
        ("bash command to count how many .txt files are anywhere under this folder",
         "```bash\nfind . -name \"*.txt\" | wc -l\n```\nfind lists the matches and wc -l counts them."),
        ("a shell pipeline that shows my ten most used commands from history",
         "```bash\nhistory | awk '{print $2}' | sort | uniq -c | sort -rn | head -n 10\n```\nIt pulls out the command names, counts repeats, and shows the ten biggest counts."),
        ("grep command to search all the .log files here for the word timeout, ignoring case",
         "```bash\ngrep -i \"timeout\" *.log\n```\nThe -i flag makes the match case-insensitive, and each hit is printed with its file name."),
        ("write a sql query for the three highest paid employees",
         "```sql\nSELECT name, salary\nFROM employees\nORDER BY salary DESC\nLIMIT 3;\n```\nSorting by salary descending and limiting to 3 gives the top earners."),
        ("sql to count the orders each customer has placed",
         "```sql\nSELECT customer_id, COUNT(*) AS order_count\nFROM orders\nGROUP BY customer_id;\n```\nGROUP BY collapses the rows per customer so COUNT can tally them."),
        ("sql query that finds duplicate email addresses in a users table",
         "```sql\nSELECT email, COUNT(*) AS n\nFROM users\nGROUP BY email\nHAVING COUNT(*) > 1;\n```\nHAVING keeps only the emails that appear more than once."),
        ("what regex matches a us zip code, with the optional plus four part",
         "```\n^\\d{5}(-\\d{4})?$\n```\nThat accepts 90210 or 90210-1234 and nothing else."),
        ("give me a regex for a hex color like `#ff00aa`",
         "```\n^#(?:[0-9a-fA-F]{3}){1,2}$\n```\nIt matches a # followed by either 3 or 6 hex digits."),
        ("regex to match a 24 hour time such as `14:30`",
         "```\n^([01]\\d|2[0-3]):[0-5]\\d$\n```\nHours run 00 to 23 and minutes 00 to 59."),
        ("write a swift function that checks if a number is prime",
         "```swift\nfunc isPrime(_ n: Int) -> Bool {\n    if n < 2 { return false }\n    var i = 2\n    while i * i <= n {\n        if n % i == 0 { return false }\n        i += 1\n    }\n    return true\n}\n```\nIt only tests divisors up to the square root of n."),
        ("swift snippet to keep only the strings longer than three characters from an array called words",
         "```swift\nlet longWords = words.filter { $0.count > 3 }\n```\n`filter` returns a new array containing just the matching strings."),
        ("how do I loop over a dictionary in swift and print each key and value",
         "```swift\nfor (key, value) in scores {\n    print(\"\\(key): \\(value)\")\n}\n```\nEach pass gives you one key and its value; the order isn't guaranteed."),
        ("what does `ls -la | grep '^d'` do",
         "It lists everything in the folder in long format and keeps only the lines that start with `d`, which means directories. So you see just the subfolders."),
        ("explain `[x * 2 for x in range(5)]` in python",
         "It's a list comprehension that doubles each number from 0 to 4, producing `[0, 2, 4, 6, 8]`."),
        ("what does `git reset --soft HEAD~1` do",
         "It undoes your last commit but leaves all its changes staged, so you can edit them and commit again. Nothing is lost."),
        ("what does `chmod 755 script.sh` do",
         "It gives the owner read, write and execute permission, and gives everyone else read and execute only. That's the usual setting for a script others need to run."),
        ("explain what `arr.map(x => x * 2)` returns in javascript",
         "It returns a new array with every element doubled and leaves the original untouched. For `[1, 2, 3]` you get `[2, 4, 6]`."),
    ]
    reg("ch_code_inline", CODE, 10)

    # ------------------------------------------------------------------ 2. jokes, poems, riddles, toasts
    JOKE = [
        ("got a joke about coffee?", "Why did the coffee file a police report? It got mugged."),
        ("give me a pun about bakers", "Bakers work so hard because they knead the dough."),
        ("write a two line poem about autumn", "Leaves drift down in amber light,\nand the year exhales into the night."),
        ("I need a riddle for my kid", "Here's one: what has keys but can't open a single lock? A piano."),
        ("can you come up with a short toast for my sister's wedding", "Here's a short one: \"To the happy couple - may your love grow stronger with every year, and may your laughter always be louder than your worries. Cheers!\""),
        ("something funny about programmers please", "Why do programmers prefer dark mode? Because light attracts bugs."),
        ("make up a joke about mondays", "Monday asked for a little compassion. The weekend said, \"Sorry, I'm already gone.\""),
        ("a cat joke would be great", "Why did the cat sit on the computer? To keep an eye on the mouse."),
        ("two line poem about the sea, nothing fancy", "The tide comes in with a whispered song,\nthen carries all my cares along."),
        ("pun about gardening?", "I tried to start a garden, but it just didn't grow on me."),
        ("can you give me a riddle with an answer", "What gets wetter the more it dries? A towel."),
        ("one-liner joke about math", "Why was the equal sign so humble? It knew it wasn't less than or greater than anyone."),
        ("write a quick birthday toast for my coworker", "Try this: \"Here's to another year of great ideas, good coffee, and even better company. Happy birthday - cheers!\""),
        ("joke for a dog lover", "What do you call a dog that does magic tricks? A labracadabrador."),
        ("short rhyming couplet about the moon", "The moon hangs low, a silver thread,\nand watches over all who've bed."),
        ("make me laugh with something about the gym", "I told my trainer I wanted a six-pack. He said, \"Great - put the pizza down.\""),
        ("a pun about clocks", "I wanted to be a clockmaker, but I found it was just a matter of time."),
        ("tell me a riddle about time", "I'm always coming but never arrive - what am I? Tomorrow."),
        ("what's a funny line about houseplants", "I named my plant Fern, and it's the only one in the house that actually thrives on neglect."),
        ("quick toast for a retirement party", "Here's one: \"To a career well done and a future full of long mornings. May your only deadline be a nice dinner reservation. Congratulations!\""),
        ("write a tiny poem about rain on a window", "Raindrops race across the glass,\neach one hurrying to pass."),
        ("a joke about pizza", "Why did the pizza maker quit? He was tired of the same old routine - it was getting too cheesy."),
        ("can you invent a pun on the word bread", "I'd tell you a joke about bread, but it's a bit stale. Let's just say that one's crumby."),
        ("I need a lighthearted line about being late", "I'm not late, I'm just giving everyone else time to be early."),
        ("riddle please, something not too hard", "What has hands but can't clap? A clock."),
        ("short good-luck poem for my friend's driving test", "Mirror, signal, steady hands,\nyou've got this - go and make your plans."),
    ]
    reg("ch_joke_story", JOKE, 6)

    # ------------------------------------------------------------------ 3. inline definitions (half also offer web_search)
    DEFINE = [
        ("what does \"break the ice\" mean", "It means to ease the tension in a new or awkward situation, usually by starting a friendly conversation or making a light joke."),
        ("meaning of \"bite the bullet\"", "To bite the bullet is to face something unpleasant or difficult with courage because it can't be avoided."),
        ("what does ghosting mean when it comes to dating", "Ghosting is suddenly cutting off all contact with someone without any explanation, such as ignoring their texts and calls."),
        ("what does FOMO stand for", "FOMO stands for \"fear of missing out\" - the anxious feeling that others are having fun or opportunities that you're not part of."),
        ("what does it mean when someone says the ball is in your court", "It means it's your turn to act or respond, and the next move is up to you."),
        ("define ephemeral", "Ephemeral means lasting for a very short time, like morning mist or a fleeting trend."),
        ("what does \"sus\" mean in slang", "\"Sus\" is short for suspicious. People use it for anything that seems shady or untrustworthy."),
        ("what does \"no cap\" mean", "\"No cap\" is slang for \"no lie\" - it means the speaker is being completely honest."),
        ("what's the meaning of the phrase once in a blue moon", "It means very rarely. A blue moon is an unusual event, so something that happens once in a blue moon almost never does."),
        ("what does spill the beans mean", "To spill the beans means to reveal a secret, usually by accident."),
        ("what does it mean if something costs an arm and a leg", "It means it's extremely expensive."),
        ("what does \"per se\" mean", "\"Per se\" is Latin for \"by itself\" or \"in itself\". For example, \"The job isn't hard per se, but it's long.\""),
        ("what does RSVP stand for", "RSVP comes from the French \"répondez s'il vous plaît\", which means \"please reply\". It asks you to confirm whether you're coming."),
        ("what is a vibe check", "A vibe check is an informal way of seeing how someone feels or what the mood is. People also say it jokingly when they want to judge whether something feels right."),
        ("what does hit the sack mean", "To hit the sack is to go to bed."),
        ("what does it mean to be \"in the loop\"", "Being in the loop means you're kept informed about what's happening on a project or situation. Being \"out of the loop\" is the opposite."),
        ("what does someone mean by \"bandwidth\" in a work context", "At work, bandwidth means someone's capacity to take on more tasks. \"I don't have the bandwidth\" means they're too busy."),
        ("meaning of circle back", "To circle back means to return to a topic or person later, once you have more information or time."),
        ("define serendipity", "Serendipity is finding something good or valuable by happy accident, without looking for it."),
        ("what does TL;DR mean", "TL;DR stands for \"too long; didn't read\". It marks a short summary of a long text."),
        ("what does \"low-key\" mean when people text it", "Low-key means somewhat, quietly, or without making a big deal. \"I'm low-key tired\" means \"I'm a little tired.\""),
        ("what does it mean to have a chip on your shoulder", "It means holding a grudge or feeling resentful about something, often in a way that makes you quick to take offense."),
        ("what does ETA stand for", "ETA stands for \"estimated time of arrival\" - when someone expects to get there."),
        ("meaning of the phrase under the weather", "Under the weather means feeling slightly ill or off, usually in a mild way, like having a cold."),
        ("what's the definition of ubiquitous", "Ubiquitous means seeming to be everywhere at the same time, such as smartphones in modern cities."),
        ("what does it mean to \"read the room\"", "To read the room is to sense the mood of the people around you and adjust your behavior or words to fit it."),
        ("what does \"slay\" mean in slang", "In slang, \"slay\" means doing something exceptionally well or looking fantastic, as in \"You slayed that presentation.\""),
        ("what does \"the whole nine yards\" mean", "It means everything, or the full extent of something. \"They gave us the whole nine yards\" means they provided everything."),
    ]
    reg("ch_define_inline", DEFINE[0::2], 3)
    reg("ch_define_inline_web", DEFINE[1::2], 3, web=True)

    # ------------------------------------------------------------------ 4. short concept explanations (half with web_search offered)
    EXPL = [
        ("what is a mutex", "A mutex (mutual exclusion lock) lets only one thread use a shared resource at a time. A thread locks it before touching the data and unlocks it afterward, which prevents two threads from corrupting the data."),
        ("why does the sky turn red at sunset", "At sunset sunlight travels through much more atmosphere. Air scatters blue light away along the way, so the longer red and orange wavelengths are what reach your eyes."),
        ("what's the difference between ram and storage", "RAM is your computer's short-term working memory: very fast, but it clears when the power goes off. Storage, like an SSD, is long-term and keeps your files even when it's off, but it's slower."),
        ("what is dns", "DNS, the Domain Name System, is the internet's phone book. It turns a name like example.com into the numeric IP address your computer actually connects to."),
        ("what does a vpn do", "A VPN sends your internet traffic through an encrypted tunnel to another server. That hides your activity from the local network and masks your IP address from the sites you visit."),
        ("how does a refrigerator keep things cold", "It moves heat out rather than making cold. A fluid called a refrigerant absorbs heat inside the fridge as it evaporates, then releases it outside the fridge as it is compressed and condensed."),
        ("what is compound interest", "Compound interest is interest earned on both your original money and on the interest already added. Because of that, balances grow faster over time than with simple interest."),
        ("why are there tides", "Tides are caused mainly by the Moon's gravity pulling on Earth's oceans. The water bulges toward the Moon and on the opposite side, so most coasts get two high tides a day."),
        ("what is a git branch", "A branch is an independent line of work in a repository. You can make changes on it without affecting the main code, then merge them back when they are ready."),
        ("how does encryption work in simple terms", "Encryption scrambles information using a secret key so only someone with the right key can turn it back into readable form. Without the key, the data looks like random noise."),
        ("what's the difference between http and https", "HTTPS is HTTP with encryption added. The data between your browser and the site is scrambled, so others on the network can't read or alter it easily."),
        ("what is a black hole", "A black hole is a region of space where gravity is so strong that nothing, not even light, can escape once it crosses the boundary called the event horizon."),
        ("what is recursion in programming", "Recursion is when a function solves a problem by calling itself on a smaller version of the same problem, until it reaches a simple base case that stops the calls."),
        ("what causes thunder", "Lightning heats the air around it to an extremely high temperature in a split second. The air expands explosively, and that shock wave is the sound we hear as thunder."),
        ("what's the difference between a stock and a bond", "A stock is a small ownership share in a company, so its value goes up and down with the company. A bond is a loan to a company or government that pays interest and returns your money on a set date."),
        ("what does a cpu actually do", "The CPU is the computer's main processor. It carries out program instructions - doing arithmetic, making decisions and moving data - billions of times per second."),
        ("what is a cache", "A cache is a small, fast store of data you're likely to need again soon. Reading from it is quicker than fetching from the original, slower source, which speeds things up."),
        ("why do onions make you cry", "Cutting an onion releases a gas that reacts with the moisture in your eyes to form a mild irritant. Your eyes produce tears to wash it away."),
        ("how does a microwave heat food", "A microwave sends radio waves that make water molecules in the food vibrate. That motion creates friction and heat, which is why watery foods warm up quickest."),
        ("what's the difference between baking soda and baking powder", "Baking soda is pure sodium bicarbonate and needs an acid in the recipe to make it rise. Baking powder already contains its own acid, so it only needs moisture and heat."),
        ("why do we have time zones", "The Earth rotates, so the sun is overhead at different times in different places. Time zones keep clocks roughly in step with daylight, so noon is near midday wherever you are."),
        ("what is a neural network, briefly", "A neural network is a program made of many simple connected units, loosely inspired by the brain. It learns by adjusting the strengths of those connections from examples until it makes good predictions."),
        ("what's the difference between wifi and the internet", "The internet is the global network of connected computers. Wi-Fi is just a way to connect your device wirelessly to a router, which then connects you to the internet."),
        ("what is an index fund", "An index fund is an investment fund that holds all the stocks in a market index, like the S&P 500. It aims to match the market's performance rather than beat it, usually with low fees."),
        ("how do vaccines get tested before approval", "They go through phases: small safety checks first, then larger trials comparing the vaccine against a placebo. Regulators review the data on safety and effectiveness before approving it."),
        ("what is a hash function", "A hash function turns any input into a fixed-size string of characters. The same input always gives the same output, and it's practically impossible to work backward from the output."),
        ("what's the difference between an alligator and a crocodile", "Alligators have a wide, U-shaped snout and mostly live in freshwater, while crocodiles have a narrower V-shaped snout and tolerate saltwater. You can also see a crocodile's lower teeth when its mouth is closed."),
        ("why does bread go stale", "Stale bread isn't just drying out. The starch in it slowly recrystallizes and pushes out moisture, which makes it firm and crumbly. Warming it briefly can reverse some of that."),
    ]
    reg("ch_explain_concept", EXPL[0::2], 3)
    reg("ch_explain_concept_web", EXPL[1::2], 3, web=True)

    # ------------------------------------------------------------------ 5. dev (wording and scenarios unused in train)
    DEV_CODE = [
        ("show me how to get the length of a list in python",
         "```python\nn = len(my_list)\n```\n`len` returns the number of items in the list."),
        ("sql statement to delete all rows older than 2020 from a table called logs with a created_at date column",
         "```sql\nDELETE FROM logs\nWHERE created_at < '2020-01-01';\n```\nRun it with a SELECT first if you want to preview which rows it will remove."),
        ("regexp for a phone number in the form `555-123-4567`",
         "```\n^\\d{3}-\\d{3}-\\d{4}$\n```\nThat requires three digits, three digits and four digits, separated by dashes."),
        ("one line of bash to print today's date as year-month-day",
         "```bash\ndate +%Y-%m-%d\n```\nThe format string gives output like 2026-10-14."),
        ("js arrow function returning true when a number is even",
         "```js\nconst isEven = (n) => n % 2 === 0;\n```\nIt returns true when dividing by 2 leaves no remainder."),
        ("what does `tail -f app.log` do",
         "It shows the last lines of app.log and then keeps running, printing new lines as they are added. Press Ctrl+C to stop it."),
    ]
    reg("ch_dev_code", DEV_CODE, 3, split="dev")

    DEV_DEF = [
        ("what's the idiom \"let the cat out of the bag\" about", "It means accidentally revealing a secret that was supposed to stay hidden."),
        ("what does it mean when someone says they're \"swamped\"", "Being swamped means having so much to do that you feel overwhelmed."),
        ("explain the word meticulous", "Meticulous means showing great attention to detail and being very careful and precise."),
        ("what does \"bet\" mean as a reply in slang", "In slang, \"bet\" is a casual way of saying \"okay\" or \"sounds good\", and sometimes \"I'm on it.\""),
        ("what does \"burn the midnight oil\" mean", "It means working or studying late into the night."),
        ("meaning of pragmatic", "Pragmatic means dealing with things in a practical, realistic way rather than sticking to theory or ideals."),
    ]
    reg("ch_dev_define", DEV_DEF, 2, split="dev")
