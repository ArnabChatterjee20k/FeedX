from src.agent.content import ContentAgent

agent = ContentAgent()
chunks = [
    "What makes that all work is the web browser’s implementations of",
    "What makes a browser different from most massive code bases is their urgency . Browsers are nearly as old as any “legacy” codebase, but are not legacy, not abandoned or half-deprecated, not slated for replacement. On the contrary, they are vital to the world’s economy. Browser engineers must therefore fix and improve rather than abandon and replace. And since the character of the web itself is highly decentralized, the use cases met by browsers are to a significant extent not determined by the companies “owning” or “controlling” a particular browser. Other people—including you—can and do contribute ideas, proposals, and implementations.",
    "and the mediator of the web’s interactions, which ultimately is what makes the web’s principles real. The browser is also the implementer of the web: its sandbox keeps web browsing safe; its algorithms implement the declarative document model; its UI navigates links. Web pages load fast and react smoothly only when the browser is hyper-efficient.",
    "The Role of the Browser (#the-role-of-the-browser)",
    "Browsers and You (#browsers-and-you)",
    "The key thing to understand is that this grand experiment is not over. The essence of the web will stay, but by building web browsers you have the chance to shape its future.",
    "To me, browsers are where algorithms come to life . A browser contains a rendering engine more complex and powerful than any computer game; a full networking stack; clever data structures and parallel programming techniques; a virtual machine, an interpreted language, and a just-in-time compiler; a world-class security sandbox; and a uniquely dynamic system for storing data.",
    ". The web inverts control , with an intermediary—the browser—handling most of the rendering, and the web developer specifying rendering parameters and content to this intermediary.",
    "Browser Code Concepts (#browser-code-concepts)",
    "That can make the browser magical or frustrating—depending on whether it is doing the right thing! But that also makes a browser a pretty unusual piece of software, with unique challenges, interesting algorithms, and clever optimizations. Browsers are worth studying for the pure pleasure of it.",
]

print(
    agent.analyze_chunks(
        chunks=chunks,
        title="browser-engineering",
        url="https://browser.engineering/http.html",
        allowed_tags=["backend"],
    )
)

chunks.extend(
    [
        "Every browser engine is divided into several major subsystems: networking, parsing, style calculation, layout, painting, compositing, JavaScript execution, and storage. Each subsystem has its own performance constraints and interacts closely with the others.",
        "When a user enters a URL, the browser performs DNS resolution, establishes a network connection, downloads the resource, parses the HTML incrementally, discovers additional resources, and begins rendering before the entire page has finished downloading.",
        "Modern browsers aggressively optimize performance through caching, speculative parsing, lazy loading, resource prioritization, incremental rendering, and hardware-accelerated compositing. These optimizations are largely invisible to users but dramatically improve perceived responsiveness.",
        "JavaScript execution is coordinated with the browser's event loop. User input, timers, rendering updates, network callbacks, and asynchronous tasks are scheduled carefully to keep interfaces responsive while maintaining correctness.",
        "CSS appears declarative, but implementing it efficiently is surprisingly complex. Selector matching, inheritance, cascading rules, custom properties, animations, and media queries all contribute to the final computed styles that determine layout and rendering.",
        "Security is a defining characteristic of browsers. The same-origin policy, sandboxing, process isolation, site isolation, permission models, certificate validation, and content security policies work together to protect users from malicious websites.",
        "Browser vendors continuously balance compatibility and innovation. Every new feature must coexist with decades of historical behavior because millions of websites rely on quirks that accidentally became part of the platform.",
        "Rendering is usually incremental rather than all at once. As more HTML arrives over the network, the browser updates the DOM, recalculates layout when necessary, paints modified regions, and composites the result onto the screen.",
        "Developer tools expose many of these internal processes. Inspectors reveal the DOM tree, computed styles, network requests, JavaScript execution timelines, memory usage, rendering performance, and accessibility information.",
        "The browser is simultaneously an operating system for web applications, a networking client, a graphics engine, a JavaScript runtime, and a security boundary. Few software projects combine so many distinct areas of computer science within a single executable.",
        "Historically, browsers evolved from simple document viewers into sophisticated application platforms capable of running complex productivity software, games, collaborative editors, video conferencing tools, and machine learning workloads directly inside the browser.",
        "Not every optimization is beneficial in every scenario. Aggressive caching can serve stale content, excessive preloading wastes bandwidth, and unnecessary JavaScript execution can delay rendering. Browser engineers constantly evaluate trade-offs.",
        "One of the recurring themes throughout browser engineering is that correctness comes before optimization. Rendering a page incorrectly—even if it is faster—is almost always considered a bug because compatibility with the web platform is paramount.",
        "A useful mental model is to think of the browser as a pipeline. Network responses become parsed documents, documents become DOM trees, DOM trees combine with CSS into render trees, render trees become layouts, layouts become display lists, and display lists become pixels.",
        "Summary: browsers are among the most sophisticated applications ever written. They integrate networking, security, graphics, programming language implementation, operating systems concepts, distributed systems, and human-computer interaction into a single coherent platform.",
    ]
)
print(
    agent.analyze_chunks(
        chunks=chunks,
        title="browser-engineering",
        url="https://browser.engineering/http.html",
        allowed_tags=["backend"],
    )
)
