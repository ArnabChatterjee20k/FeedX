# FeedX
The idea is building feedline for me based on the sources I like. A recurring feed and timeline always prepared for me so that anytime I want to read something they are always ready

Built on top of
1. [Scout](https://github.com/ArnabChatterjee20k/Scout)  
2. [Domdistill](https://github.com/ArnabChatterjee20k/domdistill)
Some of the frameworks I built recently to solve this problem efficiently

# Working

Github actions trigger the jobs automatically or using the cli to run this manually
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Actions (Scheduled Triggers)                            │
│                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐   │
│  │ queue-builder    │  │ scraper.yml      │  │ content.yml  │   │
│  │ (API)            │  │                  │  │              │   │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬───────┘   │
│           │                     │                   │           │
│           ▼                     ▼                   ▼           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Shared Database (Appwrite)                  │   │
│  │                                                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                  │
│                              ▼                                  │
│                    ┌──────────────────┐                         │
│                    │ feed-builder.yml │                         │
│                    │                  │                         │
│                    └────────┬─────────┘                         │
│                             │                                   │
│                             ▼                                   │
│                    Generate HTML → Push to gh-pages             │
└─────────────────────────────────────────────────────────────────┘