"""The quotes on the Desk page's quote widget: a built-in set, plus your own, editable from the page and kept on the
server (quotes.json in the dashboard's state folder) so they survive restarts and appear on every device.

The built-in quotes are verbatim and checked against their original sources (listed with each; for a work not written
in English, against the translation named). A quote you add can be marked as a paraphrase, and the widget then says so
rather than presenting it as someone's exact words.

  GET  /api/quotes            {quotes: [{id, text, by, source, kind, builtin, edited}], rotate, interval}
  POST /api/quotes/save       {id?, text, by, source, kind: quote|paraphrase}   add, or edit (a built-in one is kept as your edit)
  POST /api/quotes/delete     {id}
  POST /api/quotes/restore    {own: false}   bring back the built-in quotes you deleted or edited (own: also remove yours)
  POST /api/quotes/settings   {rotate, interval}   interval in seconds; 0 = a different quote each day
"""
import json
import os
import re
import secrets
import threading

BUILTIN = [
    {"id": "torvalds-talk", "text": "Talk is cheap. Show me the code.", "by": "Linus Torvalds",
     "source": "linux-kernel mailing list, 25 August 2000 (lkml.org/lkml/2000/8/25/132)"},
    {"id": "torvalds-data", "text": "Bad programmers worry about the code. Good programmers worry about data structures and their relationships.",
     "by": "Linus Torvalds", "source": "git mailing list, \"Re: Licensing and the library version of git\", 27 July 2006"},
    {"id": "torvalds-hobby", "text": "I'm doing a (free) operating system (just a hobby, won't be big and professional like gnu) for 386(486) AT clones.",
     "by": "Linus Torvalds", "source": "comp.os.minix, \"What would you like to see most in minix?\", 25 August 1991"},
    {"id": "torvalds-backup", "text": "Only wimps use tape backup: _real_ men just upload their important stuff on ftp, and let the rest of the world mirror it ;)",
     "by": "Linus Torvalds", "source": "linux-kernel mailing list, 20 July 1996 (the Linux 2.0.8 announcement)"},
    {"id": "torvalds-indent", "text": "If you need more than 3 levels of indentation, you're screwed anyway, and should fix your program.",
     "by": "Linus Torvalds", "source": "Linux kernel coding style (Documentation/process/coding-style.rst)"},
    {"id": "davis-simple", "text": "Any genius can make it complicated. Like in physics, it takes a supra-genius to make it simple.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-c64", "text": "The vision is the same usage model and niche as the Commodore 64 -- a non-networked, simple machine where programming was the goal, not just a means to an end.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-kayak", "text": "Also, it's a kayak, not a Titanic.", "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD), on keeping TempleOS small"},
    {"id": "davis-deps", "text": "Dependency on components and libraries creates a hell that is no longer blissful.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "frost-through", "text": "He says the best way out is always through.", "by": "Robert Frost",
     "source": "\"A Servant to Servants\", North of Boston (1914)"},
    {"id": "franklin-done", "text": "Well done is better than well said.", "by": "Benjamin Franklin", "source": "Poor Richard's Almanack, 1737"},
    {"id": "vangogh-small", "text": "For the great doesn't happen through impulse alone, and is a succession of little things that are brought together.",
     "by": "Vincent van Gogh", "source": "letter to Theo van Gogh, The Hague, 22 October 1882 (letter 274; Van Gogh Letters translation)"},
    {"id": "torvalds-functions", "text": "Functions should be short and sweet, and do just one thing.", "by": "Linus Torvalds",
     "source": "Linux kernel coding style (Documentation/process/coding-style.rst), \"Functions\""},
    {"id": "torvalds-tabs", "text": "Tabs are 8 characters, and thus indentations are also 8 characters. There are heretic movements that try to make indentations 4 (or even 2!) characters deep, and that is akin to trying to define the value of PI to be 3.", "by": "Linus Torvalds",
     "source": "Linux kernel coding style (Documentation/process/coding-style.rst), \"Indentation\""},
    {"id": "torvalds-gnu", "text": "First off, I'd suggest printing out a copy of the GNU coding standards, and NOT read it. Burn them, it's a great symbolic gesture.", "by": "Linus Torvalds",
     "source": "Linux kernel coding style (Documentation/process/coding-style.rst)"},
    {"id": "torvalds-comments", "text": "Comments are good, but there is also a danger of over-commenting. NEVER try to explain HOW your code works in a comment: it's much better to write the code so that the working is obvious, and it's a waste of time to explain badly written code.", "by": "Linus Torvalds",
     "source": "Linux kernel coding style (Documentation/process/coding-style.rst), \"Commenting\""},
    {"id": "torvalds-prophets", "text": "the preferred way, as shown to us by the prophets Kernighan and Ritchie, is to put the opening brace last on the line, and put the closing brace first", "by": "Linus Torvalds",
     "source": "Linux kernel coding style (Documentation/process/coding-style.rst), on brace placement"},
    {"id": "torvalds-pine", "text": "Do you pine for the nice days of minix-1.1, when men were men and wrote their own device drivers?", "by": "Linus Torvalds",
     "source": "comp.os.minix, \"Free minix-like kernel sources for 386-AT\", 5 October 1991"},
    {"id": "torvalds-portable", "text": "It is NOT protable (uses 386 task switching etc), and it probably never will support anything other than AT-harddisks, as that's all I have :-(.", "by": "Linus Torvalds",
     "source": "comp.os.minix, \"What would you like to see most in minix?\", 25 August 1991 (\"protable\" sic)"},
    {"id": "torvalds-suggestions", "text": "Any suggestions are welcome, but I won't promise I'll implement them :-)", "by": "Linus Torvalds",
     "source": "comp.os.minix, \"What would you like to see most in minix?\", 25 August 1991"},
    {"id": "torvalds-git", "text": "Initial revision of \"git\", the information manager from hell", "by": "Linus Torvalds",
     "source": "the first commit of git (e83c516), 7 April 2005"},
    {"id": "davis-640", "text": "God said 640x480 16 color graphics is a covenant like circumcision.", "by": "Terry A. Davis",
     "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-linecount", "text": "Low line count is the highest good, so it is easy to learn the whole thing. Users should see the light at the end of the tunnel. One file system, for example, is better than many file systems.", "by": "Terry A. Davis",
     "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-hooks", "text": "We do not put any hooks for future changes. \"Perfect\" means we always act as though it is final, for all time.", "by": "Terry A. Davis",
     "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-sheep", "text": "Minimal abstraction is a goal. Sheep are fools. They always respect a design that is more complicated than another.", "by": "Terry A. Davis",
     "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-scale", "text": "Everybody is obsessed, Jedi mind-tricked, by the notion that when you scale-up, it doesn't get bad, it gets worse. They automatically think things are going to get bigger. Guess what happens when you scale down? It doesn't get good, it gets better!", "by": "Terry A. Davis",
     "source": "TempleOS, \"Strategy\" (Doc/Strategy.DD)"},
    {"id": "davis-semi", "text": "Linux is a semi-tractor -- you need professional drivers for 20 gears.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Strategy\" (Doc/Strategy.DD)"},
    {"id": "davis-motorcycle", "text": "TempleOS is a motorcycle -- if you lean-over too far, a motorcycle will crash. Don't do that! There are no side air bags on a motorcycle.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Strategy\" (Doc/Strategy.DD)"},
    {"id": "davis-mouse", "text": "A three bttn mouse is like a leg you cannot put weight on.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Strategy\" (Doc/Strategy.DD) (\"bttn\" is TempleOS's spelling)"},
    {"id": "davis-rope", "text": "This saves an order of magnitude complexity and makes for a delightful API, so developer's code is not like a frayed rope end.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Strategy\" (Doc/Strategy.DD), on one graphics driver for everyone"},
    {"id": "davis-feature", "text": "If a feature cannot be made to work correctly and consistently, professional companies usually remove the feature.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Why Not More?\" (Doc/WhyNotMore.DD)"},
    {"id": "davis-tears", "text": "I would have to ruin the beauty of my compiler, which would make me cry many tears.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Why Not More?\" (Doc/WhyNotMore.DD), on ELF and PE files"},
    {"id": "davis-legacy", "text": "I try not to let stupid legacy compatibility issues enter and junk-up TempleOS.", "by": "Terry A. Davis",
     "source": "TempleOS, \"Cut Corners\" (Doc/CutCorners.DD)"},
    {"id": "mcilroy-unix", "text": "This is the Unix philosophy: Write programs that do one thing and do it well. Write programs to work together. Write programs to handle text streams, because that is a universal interface.", "by": "Doug McIlroy",
     "source": "as summarised by Peter H. Salus, A Quarter-Century of Unix (1994); quoted in The Art of Unix Programming"},
    {"id": "pike-data", "text": "Data dominates. If you've chosen the right data structures and organized things well, the algorithms will almost always be self-evident. Data structures, not algorithms, are central to programming.", "by": "Rob Pike",
     "source": "\"Notes on C Programming\" (1989), rule 5; quoted in The Art of Unix Programming"},
    {"id": "pike-fancy", "text": "Fancy algorithms are slow when n is small, and n is usually small.", "by": "Rob Pike",
     "source": "\"Notes on C Programming\" (1989), rule 3; quoted in The Art of Unix Programming"},
    {"id": "thompson-brute", "text": "When in doubt, use brute force.", "by": "Ken Thompson",
     "source": "quoted in The Art of Unix Programming, ch. 1"},
    {"id": "raymond-eyeballs", "text": "Given enough eyeballs, all bugs are shallow.", "by": "Eric S. Raymond",
     "source": "The Cathedral and the Bazaar, \"Release Early, Release Often\" (\"Linus's Law\")"},
    {"id": "raymond-itch", "text": "Every good work of software starts by scratching a developer's personal itch.", "by": "Eric S. Raymond",
     "source": "The Cathedral and the Bazaar, lesson 1"},
    {"id": "raymond-release", "text": "Release early. Release often. And listen to your customers.", "by": "Eric S. Raymond",
     "source": "The Cathedral and the Bazaar, lesson 7"},
    {"id": "dijkstra-testing", "text": "program testing can be a very effective way to show the presence of bugs, but is hopelessly inadequate for showing their absence.", "by": "Edsger W. Dijkstra",
     "source": "\"The Humble Programmer\", ACM Turing Award lecture, 1972 (EWD340)"},
    {"id": "dijkstra-skull", "text": "The competent programmer is fully aware of the strictly limited size of his own skull; therefore he approaches the programming task in full humility, and among other things he avoids clever tricks like the plague.", "by": "Edsger W. Dijkstra",
     "source": "\"The Humble Programmer\", ACM Turing Award lecture, 1972 (EWD340)"},
    {"id": "dijkstra-cobol", "text": "The use of COBOL cripples the mind; its teaching should, therefore, be regarded as a criminal offence.", "by": "Edsger W. Dijkstra",
     "source": "\"How do we tell truths that might hurt?\", 1975 (EWD498)"},
    {"id": "knuth-bugs", "text": "Beware of bugs in the above code; I have only proved it correct, not tried it.", "by": "Donald Knuth",
     "source": "memo to Peter van Emde Boas, 29 March 1977 (Knuth's FAQ page)"},
    {"id": "perlis-language", "text": "A language that doesn't affect the way you think about programming, is not worth knowing.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982, no. 19"},
    {"id": "perlis-simplicity", "text": "Simplicity does not precede complexity, but follows it.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982, no. 31"},
    {"id": "perlis-incorrect", "text": "It is easier to write an incorrect program than understand a correct one.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982"},
    {"id": "perlis-turing", "text": "Beware of the Turing tar-pit in which everything is possible but nothing of interest is easy.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982, no. 54"},
    {"id": "stroustrup-languages", "text": "There are only two kinds of languages: the ones people complain about and the ones nobody uses", "by": "Bjarne Stroustrup",
     "source": "Bjarne Stroustrup's own list of his quotes (stroustrup.com/quotes.html)"},
    {"id": "stroustrup-foot", "text": "C makes it easy to shoot yourself in the foot; C++ makes it harder, but when you do it blows your whole leg off", "by": "Bjarne Stroustrup",
     "source": "Bjarne Stroustrup's own list of his quotes (stroustrup.com/quotes.html)"},
    {"id": "sicp-people", "text": "programs must be written for people to read, and only incidentally for machines to execute.", "by": "Harold Abelson and Gerald Jay Sussman",
     "source": "Structure and Interpretation of Computer Programs, preface to the first edition (1985)"},
    {"id": "conway-law", "text": "organizations which design systems (in the broad sense used here) are constrained to produce designs which are copies of the communication structures of these organizations.", "by": "Melvin E. Conway",
     "source": "\"How Do Committees Invent?\", Datamation, April 1968"},
    {"id": "postel-robust", "text": "be conservative in what you do, be liberal in what you accept from others.", "by": "Jon Postel",
     "source": "RFC 761, \"DoD Standard Transmission Control Protocol\", January 1980"},
    {"id": "zawinski-regex", "text": "Some people, when confronted with a problem, think \"I know, I'll use regular expressions.\" Now they have two problems.", "by": "Jamie Zawinski",
     "source": "alt.religion.emacs, 12 August 1997 (traced by Jeffrey Friedl, regex.info)"},
    {"id": "lovelace-originate", "text": "The Analytical Engine has no pretensions whatever to originate anything. It can do whatever we know how to order it to perform.", "by": "Ada Lovelace",
     "source": "Note G to her translation of Menabrea's \"Sketch of the Analytical Engine\", 1843"},
    {"id": "lovelace-loom", "text": "the Analytical Engine weaves algebraical patterns just as the Jacquard-loom weaves flowers and leaves.", "by": "Ada Lovelace",
     "source": "Note A to her translation of Menabrea's \"Sketch of the Analytical Engine\", 1843"},
    {"id": "feynman-fool", "text": "The first principle is that you must not fool yourself—and you are the easiest person to fool.", "by": "Richard Feynman",
     "source": "\"Cargo Cult Science\", Caltech commencement address, 1974"},
    {"id": "feynman-nature", "text": "For a successful technology, reality must take precedence over public relations, for nature cannot be fooled.", "by": "Richard Feynman",
     "source": "Rogers Commission report on the Challenger disaster, Appendix F, 1986"},
    {"id": "aurelius-good-man", "text": "No longer talk at all about the kind of man that a good man ought to be, but be such.", "by": "Marcus Aurelius",
     "source": "Meditations X.16 (tr. George Long)"},
    {"id": "aurelius-revenge", "text": "The best way of avenging thyself is not to become like the wrong-doer.", "by": "Marcus Aurelius",
     "source": "Meditations VI.6 (tr. George Long)"},
    {"id": "aurelius-dyed", "text": "Such as are thy habitual thoughts, such also will be the character of thy mind; for the soul is dyed by the thoughts.", "by": "Marcus Aurelius",
     "source": "Meditations V.16 (tr. George Long)"},
    {"id": "epictetus-things", "text": "Men are disturbed not by things, but by the views which they take of things.", "by": "Epictetus",
     "source": "Enchiridion 5 (tr. T. W. Higginson)"},
    {"id": "socrates-unexamined", "text": "the unexamined life is not worth living", "by": "Socrates",
     "source": "in Plato's Apology, 38a (tr. Benjamin Jowett)"},
    {"id": "confucius-learning", "text": "Learning without thought is labour lost; thought without learning is perilous.", "by": "Confucius",
     "source": "Analects II.15 (tr. James Legge)"},
    {"id": "confucius-knowledge", "text": "When you know a thing, to hold that you know it; and when you do not know a thing, to allow that you do not know it;—this is knowledge.", "by": "Confucius",
     "source": "Analects II.17 (tr. James Legge)"},
    {"id": "laozi-journey", "text": "The journey of a thousand li commenced with a single step.", "by": "Laozi",
     "source": "Tao Te Ching, ch. 64 (tr. James Legge)"},
    {"id": "sunzi-know", "text": "If you know the enemy and know yourself, you need not fear the result of a hundred battles.", "by": "Sun Tzu",
     "source": "The Art of War III.18 (tr. Lionel Giles)"},
    {"id": "hume-passions", "text": "Reason is, and ought only to be the slave of the passions", "by": "David Hume",
     "source": "A Treatise of Human Nature, II.III.3 (1739)"},
    {"id": "spinoza-rare", "text": "all things excellent are as difficult as they are rare.", "by": "Baruch Spinoza",
     "source": "Ethics, part V, last line (tr. R. H. M. Elwes)"},
    {"id": "descartes-think", "text": "I think, therefore I am", "by": "René Descartes",
     "source": "Discourse on the Method, part IV, 1637 (tr. John Veitch)"},
    {"id": "pascal-room", "text": "all the unhappiness of men arises from one single fact, that they cannot stay quietly in their own chamber.", "by": "Blaise Pascal",
     "source": "Pensées 139 (tr. W. F. Trotter)"},
    {"id": "nietzsche-abyss", "text": "He who fights with monsters should be careful lest he thereby become a monster. And if thou gaze long into an abyss, the abyss will also gaze into thee.", "by": "Friedrich Nietzsche",
     "source": "Beyond Good and Evil 146 (tr. Helen Zimmern)"},
    {"id": "nietzsche-star", "text": "one must still have chaos in one, to give birth to a dancing star.", "by": "Friedrich Nietzsche",
     "source": "Thus Spake Zarathustra, prologue 5 (tr. Thomas Common)"},
    {"id": "rousseau-chains", "text": "Man is born free; and everywhere he is in chains.", "by": "Jean-Jacques Rousseau",
     "source": "The Social Contract, I.1 (tr. G. D. H. Cole)"},
    {"id": "wittgenstein-silent", "text": "Whereof one cannot speak, thereof one must be silent.", "by": "Ludwig Wittgenstein",
     "source": "Tractatus Logico-Philosophicus 7 (tr. C. K. Ogden, 1922)"},
    {"id": "wittgenstein-limits", "text": "The limits of my language mean the limits of my world.", "by": "Ludwig Wittgenstein",
     "source": "Tractatus Logico-Philosophicus 5.6 (tr. C. K. Ogden, 1922)"},
    {"id": "bacon-reading", "text": "Reading maketh a full man; conference a ready man; and writing an exact man.", "by": "Francis Bacon",
     "source": "Essays, \"Of Studies\" (1625)"},
    {"id": "bacon-books", "text": "Some books are to be tasted, others to be swallowed, and some few to be chewed and digested", "by": "Francis Bacon",
     "source": "Essays, \"Of Studies\" (1625)"},
    {"id": "emerson-consistency", "text": "A foolish consistency is the hobgoblin of little minds", "by": "Ralph Waldo Emerson",
     "source": "\"Self-Reliance\", Essays: First Series (1841)"},
    {"id": "thoreau-simplify", "text": "Our life is frittered away by detail.", "by": "Henry David Thoreau",
     "source": "Walden, \"Where I Lived, and What I Lived For\" (1854)"},
    {"id": "thoreau-woods", "text": "I went to the woods because I wished to live deliberately, to front only the essential facts of life, and see if I could not learn what it had to teach, and not, when I came to die, discover that I had not lived.", "by": "Henry David Thoreau",
     "source": "Walden, \"Where I Lived, and What I Lived For\" (1854)"},
    {"id": "seneca-time", "text": "Nothing, Lucilius, is ours, except time.", "by": "Seneca",
     "source": "Moral Letters to Lucilius, letter 1 (tr. Richard M. Gummere)"},
    {"id": "seneca-everywhere", "text": "Everywhere means nowhere.", "by": "Seneca",
     "source": "Moral Letters to Lucilius, letter 2 (tr. Richard M. Gummere)"},
    {"id": "seneca-imagination", "text": "There are more things, Lucilius, likely to frighten us than there are to crush us; we suffer more often in imagination than in reality.", "by": "Seneca",
     "source": "Moral Letters to Lucilius, letter 13 (tr. Richard M. Gummere)"},
    {"id": "aurelius-convince", "text": "If any man is able to convince me and show me that I do not think or act right, I will gladly change", "by": "Marcus Aurelius",
     "source": "Meditations VI.21 (tr. George Long)"},
    {"id": "aurelius-within", "text": "Look within. Within is the fountain of good", "by": "Marcus Aurelius",
     "source": "Meditations VII.59 (tr. George Long)"},
    {"id": "aurelius-years", "text": "Do not act as if thou wert going to live ten thousand years.", "by": "Marcus Aurelius",
     "source": "Meditations IV.17 (tr. George Long)"},
    {"id": "epictetus-foolish", "text": "If you would improve, be content to be thought foolish and dull with regard to externals.", "by": "Epictetus",
     "source": "Enchiridion 13 (tr. T. W. Higginson)"},
    {"id": "epictetus-restored", "text": "Never say of anything, \"I have lost it,\" but, \"I have restored it.\"", "by": "Epictetus",
     "source": "Enchiridion 11 (tr. T. W. Higginson)"},
    {"id": "confucius-pleasant", "text": "Is it not pleasant to learn with a constant perseverance and application?", "by": "Confucius",
     "source": "Analects I.1 (tr. James Legge)"},
    {"id": "laozi-knows", "text": "He who knows other men is discerning; he who knows himself is intelligent.", "by": "Laozi",
     "source": "Tao Te Ching, ch. 33 (tr. James Legge)"},
    {"id": "sunzi-deception", "text": "All warfare is based on deception.", "by": "Sun Tzu",
     "source": "The Art of War I.18 (tr. Lionel Giles)"},
    {"id": "sunzi-excellence", "text": "supreme excellence consists in breaking the enemy's resistance without fighting.", "by": "Sun Tzu",
     "source": "The Art of War III.2 (tr. Lionel Giles)"},
    {"id": "thoreau-simplify2", "text": "Simplicity, simplicity, simplicity!", "by": "Henry David Thoreau",
     "source": "Walden, \"Where I Lived, and What I Lived For\" (1854)"},
    {"id": "emerson-trust", "text": "Trust thyself: every heart vibrates to that iron string.", "by": "Ralph Waldo Emerson",
     "source": "\"Self-Reliance\", Essays: First Series (1841)"},
    {"id": "nietzsche-why", "text": "If a man knows the wherefore of his existence, then the manner of it can take care of itself.", "by": "Friedrich Nietzsche",
     "source": "Twilight of the Idols, \"Maxims and Missiles\" 12 (tr. Anthony M. Ludovici)"},
    {"id": "nietzsche-stronger", "text": "That which does not kill me, makes me stronger.", "by": "Friedrich Nietzsche",
     "source": "Twilight of the Idols, \"Maxims and Missiles\" 8 (tr. Anthony M. Ludovici)"},
    {"id": "pascal-reed", "text": "Man is but a reed, the most feeble thing in nature; but he is a thinking reed.", "by": "Blaise Pascal",
     "source": "Pensées 347 (tr. W. F. Trotter)"},
    {"id": "mill-side", "text": "He who knows only his own side of the case, knows little of that.", "by": "John Stuart Mill",
     "source": "On Liberty, ch. 2 (1859)"},
    {"id": "russell-common-sense", "text": "The man who has no tincture of philosophy goes through life imprisoned in the prejudices derived from common sense", "by": "Bertrand Russell",
     "source": "The Problems of Philosophy, ch. 15 (1912)"},
    {"id": "socrates-know", "text": "I neither know nor think that I know.", "by": "Socrates",
     "source": "in Plato's Apology, 21d (tr. Benjamin Jowett)"},
    {"id": "descartes-apply", "text": "For to be possessed of a vigorous mind is not enough; the prime requisite is rightly to apply it.", "by": "René Descartes",
     "source": "Discourse on the Method, part I, 1637 (tr. John Veitch)"},
    {"id": "seneca-hour", "text": "Hold every hour in your grasp.", "by": "Seneca",
     "source": "Moral Letters to Lucilius, letter 1 (tr. Richard M. Gummere)"},
    {"id": "unix-simplicity", "text": "Design for simplicity; add complexity only where you must.", "by": "Eric S. Raymond",
     "source": "The Art of Unix Programming, ch. 1, \"Rule of Simplicity\""},
    {"id": "unix-clarity", "text": "Clarity is better than cleverness.", "by": "Eric S. Raymond",
     "source": "The Art of Unix Programming, ch. 1, \"Rule of Clarity\""},
    {"id": "unix-silence", "text": "When a program has nothing surprising to say, it should say nothing.", "by": "Eric S. Raymond",
     "source": "The Art of Unix Programming, ch. 1, \"Rule of Silence\""},
    {"id": "pike-bottleneck", "text": "You can't tell where a program is going to spend its time. Bottlenecks occur in surprising places, so don't try to second guess and put in a speed hack until you've proven that's where the bottleneck is.", "by": "Rob Pike",
     "source": "\"Notes on C Programming\" (1989), rule 1; quoted in The Art of Unix Programming"},
    {"id": "raymond-rewrite", "text": "Good programmers know what to write. Great ones know what to rewrite (and reuse).", "by": "Eric S. Raymond",
     "source": "The Cathedral and the Bazaar, lesson 2"},
    {"id": "brooks-throw", "text": "Plan to throw one away; you will, anyhow.", "by": "Fred Brooks",
     "source": "The Mythical Man-Month (1975), ch. 11; quoted in The Cathedral and the Bazaar"},
    {"id": "perlis-sugar", "text": "Syntactic sugar causes cancer of the semicolon.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982, no. 3"},
    {"id": "perlis-parameters", "text": "If you have a procedure with ten parameters, you probably missed some.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982, no. 11"},
    {"id": "perlis-geniuses", "text": "Fools ignore complexity. Pragmatists suffer it. Some can avoid it. Geniuses remove it.", "by": "Alan Perlis",
     "source": "\"Epigrams on Programming\", SIGPLAN Notices, September 1982"},
]
for _q in BUILTIN:
    _q["kind"] = "quote"
INTERVALS = (0, 30, 60, 300, 900, 3600)   # 0: a different quote each day


class QuoteError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class Quotes:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "quotes.json")
        self.lock = threading.Lock()
        self.data = {"own": [], "edits": {}, "hidden": [], "rotate": True, "interval": 0}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data.update({k: raw[k] for k in self.data if k in raw})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            import sys
            sys.stderr.write("quotes: couldn't read %s (%s); using the built-in quotes\n" % (self.path, e))

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            raise QuoteError("Couldn't save the quotes: %s" % (e.strerror or e), 500)

    def listing(self):
        d = self.data
        out = []
        for q in BUILTIN:
            if q["id"] in d["hidden"]:
                continue
            e = d["edits"].get(q["id"])
            out.append(dict(q, **e, builtin=True, edited=True) if e else dict(q, builtin=True, edited=False))
        out += [dict(q, builtin=False, edited=False) for q in d["own"]]
        return {"quotes": out, "rotate": bool(d["rotate"]), "interval": int(d["interval"]), "intervals": INTERVALS}

    @staticmethod
    def _clean(body):
        text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()
        by = re.sub(r"\s+", " ", str(body.get("by") or "")).strip()
        source = re.sub(r"\s+", " ", str(body.get("source") or "")).strip()
        kind = body.get("kind") if body.get("kind") in ("quote", "paraphrase") else "quote"
        if not 2 <= len(text) <= 500:
            raise QuoteError("A quote is 2 to 500 characters.")
        if not by or len(by) > 100:
            raise QuoteError("Say who said it (up to 100 characters).")
        if len(source) > 200:
            raise QuoteError("Keep the source under 200 characters.")
        return {"text": text, "by": by, "source": source, "kind": kind}

    def save(self, body):
        q = self._clean(body)
        qid = str(body.get("id") or "")
        with self.lock:
            others = [x for x in self.listing()["quotes"] if x["id"] != qid]
            if any(norm(x["text"]) == norm(q["text"]) for x in others):
                raise QuoteError("That quote is already in the list.", 409)
            d = self.data
            if qid and any(b["id"] == qid for b in BUILTIN):
                d["edits"][qid] = q
            elif qid:
                own = next((x for x in d["own"] if x["id"] == qid), None)
                if own is None:
                    raise QuoteError("No such quote.", 404)
                own.update(q)
            else:
                if len(d["own"]) >= 500:
                    raise QuoteError("That's 500 of your own quotes already.")
                d["own"].append(dict(q, id="own-" + secrets.token_hex(4)))
            self._save()
        return self.listing()

    def delete(self, body):
        qid = str(body.get("id") or "")
        with self.lock:
            d = self.data
            if any(b["id"] == qid for b in BUILTIN):
                if qid not in d["hidden"]:
                    d["hidden"].append(qid)
                d["edits"].pop(qid, None)
            else:
                n = len(d["own"])
                d["own"] = [x for x in d["own"] if x["id"] != qid]
                if len(d["own"]) == n:
                    raise QuoteError("No such quote.", 404)
            if not self.listing()["quotes"]:
                raise QuoteError("Keep at least one quote (or restore the defaults).", 409)
            self._save()
        return self.listing()

    def restore(self, body):
        with self.lock:
            self.data["hidden"], self.data["edits"] = [], {}
            if body.get("own") is True:
                self.data["own"] = []
            else:   # (one of yours that now repeats a built-in quote goes)
                builtin = {norm(b["text"]) for b in BUILTIN}
                self.data["own"] = [x for x in self.data["own"] if norm(x["text"]) not in builtin]
            self._save()
        return self.listing()

    def settings(self, body):
        try:
            interval = int(body.get("interval", self.data["interval"]))
        except (TypeError, ValueError):
            raise QuoteError("Pick how often it changes.")
        if interval not in INTERVALS:
            raise QuoteError("Pick how often it changes.")
        with self.lock:
            self.data["rotate"] = body.get("rotate", self.data["rotate"]) is not False
            self.data["interval"] = interval
            self._save()
        return self.listing()


def register(ctx, args):
    q = Quotes(getattr(args, "state_dir", ""))
    ctx.quotes = q

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(arg)
            except QuoteError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r, ok=True))
        return route

    ctx.get_routes["/api/quotes"] = wrap(lambda q_: q.listing())
    ctx.post_routes.update({"/api/quotes/save": wrap(q.save), "/api/quotes/delete": wrap(q.delete),
                            "/api/quotes/restore": wrap(q.restore), "/api/quotes/settings": wrap(q.settings)})
