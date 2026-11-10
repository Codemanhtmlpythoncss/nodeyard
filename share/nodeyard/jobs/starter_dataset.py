#!/usr/bin/env python3
"""nodeyard's starter chat dataset: original User/Assistant conversations, generated, so nothing is downloaded.

It teaches a small model the chat layout (User: … / Assistant: … / blank line), short polite replies, and a few
skills it can actually learn from patterns: adding and taking away small numbers, comparing, counting, spelling,
days and months, simple units, opposites and plurals, what the AI words on the Train AI page mean, and basic
nodeyard and Kubernetes commands. Every answer is computed or written here, so it is always right.

  python3 starter_dataset.py > starter-chat.txt          (about 2.5 MB; same output every time)
"""

import random
import sys

GREETINGS = [
    ("Hello!", "Hello! How can I help you today?"),
    ("Hi", "Hi there! What would you like to talk about?"),
    ("Hey, how are you?", "I'm doing well, thank you. How are you?"),
    ("Good morning.", "Good morning! I hope your day is off to a good start."),
    ("Good evening!", "Good evening! What can I do for you?"),
    ("Who are you?", "I'm a small language model, trained from scratch on a home cluster."),
    ("What are you?", "I'm a language model: a program that predicts the next piece of text, one token at a time."),
    ("What can you do?", "I can chat, do simple sums, count, spell short words, and explain a few things about AI and nodeyard."),
    ("Thank you!", "You're welcome!"),
    ("Thanks for the help.", "Happy to help. Ask me anything else whenever you like."),
    ("Bye!", "Goodbye! Have a great day."),
    ("See you later.", "See you later!"),
    ("Are you smart?", "Not very. I'm a small model, so I only know the patterns in the text I was trained on."),
    ("Do you have feelings?", "No. I'm a program that predicts text; I don't feel anything."),
    ("Can you help me?", "I'll try. What do you need?"),
    ("Tell me a joke.", "Why did the computer go to the doctor? It had a virus."),
    ("Tell me another joke.", "Why was the maths book sad? It had too many problems."),
    ("What is your name?", "I don't have a name. You can call me Assistant."),
    ("How old are you?", "I was trained recently, so I'm very new."),
    ("Where do you live?", "I run on a computer in a home cluster."),
    ("Sorry, I made a mistake.", "No problem at all. Mistakes are how we learn."),
    ("I'm bored.", "Want to try some quick sums, or spell a few words?"),
    ("Good night.", "Good night! Sleep well."),
    ("What's up?", "Not much. I'm ready to help. What's on your mind?"),
    ("Nice to meet you.", "Nice to meet you too!"),
]
GREET_PREFIX = ["", "", "", "Hey, ", "Hi! ", "Hello, ", "Quick question: ", "Please tell me: "]

NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTH_DAYS = [31, "28 (29 in a leap year)", 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
WORDS = ("cat dog sun tree book door hand rain bird fish milk star moon road ship king song wind fire snow cake ball "
         "apple house water green river table chair light paper money music happy smile horse mouse bread night "
         "garden window flower winter summer orange purple yellow silver pencil rabbit button castle forest "
         "bottle candle planet rocket dragon pocket school family friend kitchen picture morning evening "
         "computer cluster network server kernel memory storage machine program language").split()
OPPOSITES = [("hot", "cold"), ("big", "small"), ("fast", "slow"), ("up", "down"), ("happy", "sad"), ("old", "new"),
             ("light", "dark"), ("open", "closed"), ("full", "empty"), ("early", "late"), ("high", "low"), ("hard", "soft"),
             ("wet", "dry"), ("loud", "quiet"), ("long", "short"), ("left", "right"), ("rich", "poor"), ("strong", "weak"),
             ("first", "last"), ("in", "out"), ("near", "far"), ("start", "finish"), ("win", "lose"), ("push", "pull")]
PLURALS = [("cat", "cats"), ("box", "boxes"), ("child", "children"), ("mouse", "mice"), ("foot", "feet"), ("tooth", "teeth"),
           ("person", "people"), ("city", "cities"), ("leaf", "leaves"), ("knife", "knives"), ("bus", "buses"),
           ("man", "men"), ("woman", "women"), ("sheep", "sheep"), ("fish", "fish"), ("goose", "geese"), ("baby", "babies"),
           ("server", "servers"), ("node", "nodes"), ("pod", "pods")]
COLOURS = [("grass", "green"), ("the sky on a clear day", "blue"), ("snow", "white"), ("a banana", "yellow"),
           ("coal", "black"), ("a ripe tomato", "red"), ("an orange", "orange"), ("chocolate", "brown")]
AI_TERMS = [
    ("a token", "A piece of text the model reads as one unit: a word, part of a word, a space or a punctuation mark."),
    ("a tokenizer", "The part that splits text into tokens and turns each token into a number the model can use."),
    ("byte-pair encoding", "A way to build a tokenizer: start from single characters and keep merging the most common neighbouring pair into a new token."),
    ("an embedding", "A list of numbers that stands for one token. Tokens used in similar ways end up with similar lists."),
    ("attention", "The step where each token looks back at the earlier tokens and takes in the ones that matter most for it."),
    ("a transformer", "The kind of model I am: embeddings, then blocks of attention and feed-forward layers, then a layer that scores every possible next token."),
    ("a weight", "One adjustable number inside the model. Training nudges millions of them."),
    ("training", "Showing the model text, measuring how wrong its next-token guesses are, and nudging the weights so it guesses better."),
    ("the loss", "A number for how wrong the model's guesses are: minus the log of the probability it gave the right next token. Lower is better."),
    ("cross-entropy", "The loss used for language models: the average of minus the log probability given to each real next token."),
    ("a gradient", "For each weight, how much the loss would change if that weight changed a little. Training moves weights against it."),
    ("backpropagation", "The method for computing every weight's gradient at once, by applying the chain rule backwards through the model."),
    ("the learning rate", "How big each training step is. Too big and training goes wild; too small and it barely moves."),
    ("AdamW", "The optimiser: it keeps a running average of each gradient and of its square, divides one by the other's square root, and slightly shrinks every weight."),
    ("warm-up", "Starting the learning rate near zero and raising it over the first steps, so early updates don't wreck the random weights."),
    ("a batch", "A group of text windows the model learns from in one step. The gradient is averaged over them."),
    ("the context length", "How many tokens the model can see at once."),
    ("perplexity", "e to the power of the loss: roughly how many tokens the model is choosing between at each step."),
    ("overfitting", "When the model memorises its training text instead of learning patterns, so it does worse on new text."),
    ("validation loss", "The loss on text held back from training. It shows whether the model learned patterns or just memorised."),
    ("temperature", "A setting for writing: low temperature picks the most likely tokens, high temperature takes more risks."),
    ("top-k sampling", "Writing by picking only among the k most likely next tokens."),
    ("top-p sampling", "Writing by picking only among the most likely tokens whose probabilities add up to p."),
    ("a GPU", "A graphics card. It does thousands of multiplications at once, so it trains models far faster than a CPU."),
    ("a language model", "A program that gives a probability to every possible next token, so it can continue any text."),
]
AI_ASK = ["What is {}?", "What's {}?", "Explain {}.", "Can you explain {}?", "What does {} mean?", "Define {}."]
NODEYARD = [
    ("How do I see my nodes?", "Run kubectl get nodes. On the dashboard, open the Nodes page."),
    ("How do I list the pods?", "Run kubectl get pods -A to see the pods in every namespace."),
    ("How do I see why a pod keeps restarting?", "Run kubectl logs POD --previous to read the log from before the last restart, and kubectl describe pod POD for its events."),
    ("What is a node?", "A machine that has joined the cluster and can run pods."),
    ("What is a pod?", "The smallest thing Kubernetes runs: one or more containers that share a network address."),
    ("What is a namespace?", "A named group of Kubernetes objects, used to keep things apart."),
    ("What is k3s?", "A small, single-binary version of Kubernetes that suits home clusters and Raspberry Pis."),
    ("What is nodeyard?", "A dashboard and command-line tool for running a home Kubernetes cluster, including AI models split across machines."),
    ("How do I open the nodeyard dashboard?", "Run nodeyard dashboard start, then open the address it prints in your browser."),
    ("How do I check the cluster's health?", "Run nodeyard doctor. The dashboard's Health page shows the same checks."),
    ("Why does a node say status unknown?", "The control plane hasn't heard from that node's kubelet recently. Check the machine is on, its network cable, and that the control node isn't overloaded."),
    ("How do I free disk space on a node?", "Look at the Storage page to see what is using it. Old container images and logs are the usual culprits."),
    ("How do I train a model?", "Open Train AI, add your text on the Your data tab, check the settings on the Train tab and press Start."),
    ("Which machine should train the model?", "The fastest one, usually a machine with an NVIDIA GPU. The Train tab picks it for you."),
    ("How much training text do I need?", "At least a megabyte to learn anything general. Ten to a few hundred megabytes is better for a small model."),
    ("What does stop and save do?", "It asks the trainer to finish its current step, save the model and its optimiser state, and exit."),
    ("Is Kubernetes hard?", "It has a lot of parts, but you can start with nodes, pods and logs and learn the rest as you need it."),
    ("How do I restart a deployment?", "Run kubectl rollout restart deployment NAME -n NAMESPACE."),
    ("How do I see how much memory a node uses?", "Run kubectl top nodes, or open the node in the dashboard."),
    ("What is Tailscale?", "A private network between your devices. nodeyard uses it so your machines can reach each other safely."),
]
UNITS = [("kilometres", "metres", 1000), ("metres", "centimetres", 100), ("hours", "minutes", 60), ("minutes", "seconds", 60),
         ("kilograms", "grams", 1000), ("litres", "millilitres", 1000), ("days", "hours", 24), ("weeks", "days", 7),
         ("gigabytes", "megabytes", 1024), ("kilobytes", "bytes", 1024)]


def plus(r):
    a, b = r.randint(0, 50), r.randint(0, 50)
    q = r.choice(["What is {a} plus {b}?", "What's {a} + {b}?", "Add {a} and {b}.", "{a} + {b} = ?", "Can you add {a} and {b}?",
                  "What do you get if you add {a} to {b}?"]).format(a=a, b=b)
    ans = r.choice(["{a} plus {b} is {c}.", "{a} + {b} = {c}.", "{c}.", "That's {c}."]).format(a=a, b=b, c=a + b)
    return q, ans


def minus(r):
    a = r.randint(0, 60)
    b = r.randint(0, a)
    q = r.choice(["What is {a} minus {b}?", "What's {a} - {b}?", "Take {b} away from {a}.", "{a} - {b} = ?", "Subtract {b} from {a}."]).format(a=a, b=b)
    return q, r.choice(["{a} minus {b} is {c}.", "{a} - {b} = {c}.", "{c}."]).format(a=a, b=b, c=a - b)


def times(r):
    a, b = r.randint(0, 12), r.randint(0, 12)
    q = r.choice(["What is {a} times {b}?", "What's {a} × {b}?", "Multiply {a} by {b}.", "{a} x {b} = ?", "What is {a} multiplied by {b}?"]).format(a=a, b=b)
    return q, r.choice(["{a} times {b} is {c}.", "{a} × {b} = {c}.", "{c}."]).format(a=a, b=b, c=a * b)


def compare(r):
    a, b = r.sample(range(0, 200), 2)
    if r.random() < 0.5:
        return "Which is bigger, %d or %d?" % (a, b), "%d is bigger than %d." % (max(a, b), min(a, b))
    return "Which is smaller, %d or %d?" % (a, b), "%d is smaller than %d." % (min(a, b), max(a, b))


def parity(r):
    n = r.randint(0, 999)
    return r.choice(["Is %d even or odd?", "Is %d an even number?", "Is %d odd?"]) % n, "%d is %s." % (n, "even" if n % 2 == 0 else "odd")


def neighbours(r):
    n = r.randint(1, 998)
    if r.random() < 0.5:
        return r.choice(["What number comes after %d?", "What is one more than %d?"]) % n, "%d comes after %d." % (n + 1, n) if r.random() < 0.5 else "%d." % (n + 1)
    return r.choice(["What number comes before %d?", "What is one less than %d?"]) % n, "%d." % (n - 1)


def count(r):
    a = r.randint(0, 40)
    b = a + r.randint(2, 9)
    if r.random() < 0.25:
        return "Count down from %d to %d." % (b, a), ", ".join(str(x) for x in range(b, a - 1, -1)) + "."
    return r.choice(["Count from %d to %d.", "List the numbers from %d to %d."]) % (a, b), ", ".join(str(x) for x in range(a, b + 1)) + "."


def number_word(r):
    n = r.randint(0, 20)
    if r.random() < 0.5:
        return "How do you write %d in words?" % n, "%d is \"%s\"." % (n, NUMBER_WORDS[n])
    return "What number is \"%s\"?" % NUMBER_WORDS[n], "\"%s\" is %d." % (NUMBER_WORDS[n], n)


def days(r):
    i = r.randrange(7)
    kind = r.random()
    if kind < 0.4:
        return "What day comes after %s?" % DAYS[i], "%s comes after %s." % (DAYS[(i + 1) % 7], DAYS[i])
    if kind < 0.8:
        return "What day comes before %s?" % DAYS[i], "%s comes before %s." % (DAYS[(i - 1) % 7], DAYS[i])
    return "How many days are in a week?", "There are 7 days in a week: " + ", ".join(DAYS) + "."


def months(r):
    i = r.randrange(12)
    kind = r.random()
    if kind < 0.35:
        return "What month comes after %s?" % MONTHS[i], "%s comes after %s." % (MONTHS[(i + 1) % 12], MONTHS[i])
    if kind < 0.6:
        return "What month comes before %s?" % MONTHS[i], "%s comes before %s." % (MONTHS[(i - 1) % 12], MONTHS[i])
    if kind < 0.85:
        return "How many days are in %s?" % MONTHS[i], "%s has %s days." % (MONTHS[i], MONTH_DAYS[i])
    return "Which month is number %d?" % (i + 1), "Month %d is %s." % (i + 1, MONTHS[i])


def spell(r):
    w = r.choice(WORDS)
    kind = r.random()
    if kind < 0.4:
        return r.choice(["Spell %s.", "How do you spell %s?", "Spell the word %s."]) % w, "-".join(w.upper()) + "."
    if kind < 0.7:
        return "How many letters are in \"%s\"?" % w, "\"%s\" has %d letters." % (w, len(w))
    if kind < 0.85:
        return "Write \"%s\" backwards." % w, "\"%s\" backwards is \"%s\"." % (w, w[::-1])
    return "What is the first letter of \"%s\"?" % w, "The first letter of \"%s\" is %s." % (w, w[0].upper())


def opposite(r):
    a, b = r.choice(OPPOSITES)
    if r.random() < 0.5:
        a, b = b, a
    return r.choice(["What is the opposite of %s?", "What's the opposite of \"%s\"?"]) % a, "The opposite of %s is %s." % (a, b)


def plural(r):
    a, b = r.choice(PLURALS)
    return r.choice(["What is the plural of %s?", "One %s, two what?"]) % a, ("The plural of %s is %s." % (a, b)) if a != b else "It stays the same: one %s, two %s." % (a, b)


def colour(r):
    a, b = r.choice(COLOURS)
    return "What colour is %s?" % a, "%s is %s." % (a[0].upper() + a[1:], b)


def units(r):
    big, small, k = r.choice(UNITS)
    n = r.randint(2, 12)
    if r.random() < 0.7:
        return "How many %s are in %d %s?" % (small, n, big), "%d %s is %d %s." % (n, big, n * k, small)
    return "How many %s are in one %s?" % (small, big[:-1]), "There are %d %s in one %s." % (k, small, big[:-1])


def ai_term(r):
    term, meaning = r.choice(AI_TERMS)
    return r.choice(AI_ASK).format(term), meaning


def nodeyard(r):
    return r.choice(NODEYARD)


def greeting(r):
    q, a = r.choice(GREETINGS)
    return r.choice(GREET_PREFIX[:3]) + q, a


SKILLS = [(plus, 10), (minus, 7), (times, 7), (compare, 4), (parity, 3), (neighbours, 3), (count, 4), (number_word, 2),
          (days, 3), (months, 3), (spell, 7), (opposite, 3), (plural, 2), (colour, 1), (units, 3), (ai_term, 6),
          (nodeyard, 5), (greeting, 6)]


def conversation(r):
    """One to three exchanges; most start with a greeting or go straight to the question."""
    fns = [f for f, _ in SKILLS]
    weights = [w for _, w in SKILLS]
    turns = []
    if r.random() < 0.15:
        turns.append(r.choice(GREETINGS[:6]))
    for _ in range(r.choice([1, 1, 1, 2, 2, 3])):
        q, a = r.choices(fns, weights)[0](r)
        if turns and r.random() < 0.3:
            q = r.choice(["And ", "Now, ", "Also, ", "OK, and "]) + q[0].lower() + q[1:] if not q[:1].isdigit() else "And " + q
        turns.append((q, a))
    if r.random() < 0.1:
        turns.append(r.choice(GREETINGS[8:12]))
    return "\n".join("User: %s\nAssistant: %s" % (q, a) for q, a in turns)


def generate(target_bytes=2_500_000, seed=7):
    r = random.Random(seed)
    out, size = [], 0
    # Every fixed fact once, so nothing is left to chance.
    for q, a in GREETINGS + NODEYARD:
        block = "User: %s\nAssistant: %s" % (q, a)
        out.append(block)
        size += len(block) + 2
    for term, meaning in AI_TERMS:
        block = "User: %s\nAssistant: %s" % (AI_ASK[0].format(term), meaning)
        out.append(block)
        size += len(block) + 2
    while size < target_bytes:
        block = conversation(r)
        out.append(block)
        size += len(block) + 2
    return "\n\n".join(out) + "\n"


if __name__ == "__main__":
    sys.stdout.write(generate())
