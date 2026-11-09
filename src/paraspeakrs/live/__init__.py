"""Live meeting analysis (spike).

A transcript arrives a few lines at a time. A decision model that speaks the JEV
Decisions API cuts it into sections by topic and decides, per persona, whether the
newest lines deserve a comment. A chat model writes those comments, titles the
sections, runs the questionnaire that sets up a meeting, and answers questions
about the meeting while it is still going on.

The spike replays a finished transcript as if it were live; see
docs/plans/2026-11-09-live-meeting-analysis.md for the road to live audio.
"""
