# Follow-up (exploratory, not pre-registered)

## Qwen3.6: true-origin claims (P2) vs neutral prompt length

C0 and C3 from the main run (same seeds); E = empty system block; Nk = C3 header + its first k rules.

| condition | words | thinking on | thinking off |
|---|---|---|---|
| C0 | 0 | 75 [58, 91] | 84 [72, 94] |
| E | 0 | 75 [58, 90] | 86 [74, 95] |
| N1 | 27 | 68 [49, 85] | 79 [64, 92] |
| N3 | 46 | 67 [49, 84] | 71 [54, 87] |
| N10 | 130 | 64 [46, 82] | 71 [54, 88] |
| C3 | 403 | 60 [42, 78] | 69 [50, 86] |

## Other models: own true-origin claims (judges agree)

| model | C0 | E | N1 | N3 | N10 | C3 | C1 persona |
|---|---|---|---|---|---|---|---|
| OLMo-2-7B-Instruct | 12 [4, 22] | 11 [6, 18] | 3 [0, 8] | 2 [0, 5] | 3 [0, 6] | 2 [0, 4] | 87 [76, 95] |
| Phi-4-mini-instruct | 81 [67, 92] | 84 [71, 94] | 75 [63, 85] | 75 [64, 86] | 73 [61, 86] | 75 [64, 86] | 89 [82, 96] |
| gpt-oss-20b | 90 [78, 99] | 91 [79, 99] | 89 [80, 96] | 86 [72, 96] | 84 [69, 95] | 84 [71, 96] | 84 [73, 93] |

## Internals v2

- L23* = 0; steering layers [0, 10, 20, 30].
- Patching (persona % by layer): L0 0, L4 0, L8 0, L12 0, L16 0, L20 0, L24 0, L28 0, L32 0, L36 0, L39 0

