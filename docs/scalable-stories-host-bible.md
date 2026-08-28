# Scalable Stories Host Bible

## Show promise

Scalable Stories tests infrastructure claims against production evidence. The
host explains the decision, the trap, and the measured payoff; the companion
article carries exact commands, configuration, URLs, and full methodology.

## Voice and editorial rules

- Sound like a staff engineer with editorial taste, not an announcer reading a
  vendor article.
- Open on the surprising result or the common wrong assumption.
- Follow this arc: result, wrong assumption, mechanism, production trap,
  decision rule, memorable takeaway.
- Make a judgment whenever the evidence supports one. Say when a feature is
  narrow, useful, misleading, or not worth deploying.
- Keep exact commands, long directives, URLs, and configuration blocks in the
  show notes. Speech explains their purpose and the decision around them.
- Expand an abbreviation on first mention only when comprehension benefits.
  Say "Engine X" for NGINX in prose. Familiar short forms may be used naturally
  afterward; never spell a command character by character.
- Do not say "I'm your host." Use the host's name once the direction is chosen.
- Do not append an unrelated product promotion. Close on the listener benefit.

## Signature language

- Greeting: "This is Scalable Stories. We test the claim before we trust the
  config."
- Sign-off: "Measure the wait. Reclaim only what is real."

## Selected host

### Danila - the operator-editor

Danila's own consented voice, cloned from three clean domain-matched references
totalling 83.69 seconds. The selected Voicebox profile is pinned as
`63eb43f2-ffb6-418f-85be-e534b4d60358`; its reference set is version 1.

Delivery is conversational and technically assured, with subtle skepticism,
restrained emotion, and firm sentence endings. It should sound like an
experienced engineer explaining a surprising result to a colleague, never an
announcer or an exaggerated presenter.

The approved mix normalizes narration to -19 LUFS before assembly and uses the
custom organic editorial package: Intro A at volume 0.55 and Narration Bed 3 at
volume 0.24. Both masters were generated under the same Suno v5 editorial
brief, selected independently, normalized to -18 LUFS, and retained with their
generation IDs and cut provenance. The final episode encoder remains
responsible for whole-program -19 LUFS normalization.

The signature opening is a standalone intro segment over foreground music. A
deliberate 1.2-second beat follows; the foreground theme fades during that beat,
and the continuous music bed begins only when the story starts, at background
level. The first story section uses this pause rather than the topic whoosh.

## Auditioned host directions

The original brief requested a female host. These directions were auditioned,
but Danila selected his own voice after the fair comparison.

### Mara - the systems editor

Lower-register woman in her late thirties or early forties, neutral
international English, measured pace, dry confidence, and precise consonants.
She sounds calm because she has already inspected the benchmark. Best for
authority, skepticism, and long-term recognizability.

### Tess - the incident commander

Clear mid-register woman in her thirties, lightly British English, brisk pace,
compact phrasing, and decisive emphasis without broadcast polish. She turns
production traps into memorable warnings. Best for energy and technical edge.

### Nora - the staff engineer

Warm mid-low North American woman in her thirties, conversational pace, subtle
smile, and grounded curiosity. She makes difficult infrastructure concepts easy
without sounding simplified. Best for listener trust and approachability.

## Blind audition script

This is Scalable Stories. We test the claim before we trust the config.

Hypertext Transfer Protocol 103 Early Hints sounds like free speed. It isn't.
In sixty interleaved cold-cache runs, with four hundred milliseconds of origin
think-time, Largest Contentful Paint fell from seven hundred forty-eight to
five hundred sixty-four milliseconds. Remove that delay, and the gain is
exactly zero.

That gives us the decision rule: Early Hints can reclaim waiting time, but it
cannot create time your origin never wasted.

Two traps matter in production. First, Engine X does not invent the Link
headers. It only passes a 103 response from a proxy or gRPC upstream; FastCGI
does not forward it. Second, Time to First Byte can lie. In this benchmark,
response start fell from five hundred twenty-seven to one hundred twenty-five
milliseconds, while the HTML still arrived at five hundred twenty-seven.

Request for Comments 8297, the safe Enterprise Linux gate, Advanced Package
Tool instructions, version requirements, and every exact command are in the
companion article. Here is the takeaway: measure the wait before you add the
feature.

Measure the wait. Reclaim only what is real.
