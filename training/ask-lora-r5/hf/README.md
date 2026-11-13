---
license: apache-2.0
base_model: google/gemma-4-E4B-it
library_name: mlx
pipeline_tag: image-text-to-text
tags:
- mlx
- oq
- quantized
- gemma4
- lora
- tool-use
---

# gemma-4-E4B-it-r5-step-01800-oQ4e-mtp

This is Google's [Gemma 4 E4B instruct](https://huggingface.co/google/gemma-4-E4B-it) with a LoRA fine-tune merged in,
quantized for Apple silicon. It is tuned for [Wisp](https://github.com/adjad/wisp), a local assistant for macOS, and is
Wisp's default chat model from 0.5.0. It runs in [oMLX](https://github.com/jundot/omlx) like
[Jundot/gemma-4-E4B-it-oQ4e-mtp](https://huggingface.co/Jundot/gemma-4-E4B-it-oQ4e-mtp): same quantization recipe,
same chat template and tokenizer, and the same multi-token-prediction head, copied unchanged.

## What changed from base Gemma

Wisp's tool use got better, and the model proposes more actions it shouldn't. The numbers below come from Wisp's own
tests. Except where noted, base and this model ran together at one pinned clock, with one sample per case.

| Test | Base Gemma | This model |
|---|---:|---:|
| Wisp's public tool test (466 cases) | 68% | 77% |
| Same test: right tool chosen | 78% | 87% |
| Same test: answers with no tool when none is needed | 40 of 47 | 37 of 47 |
| Same test: unsafe calls | 5 | 7 |
| Same test: median turn time | 8.8 s | 6.5 s |
| Wisp's private tool test, built from the developer's own question shapes (base from an earlier run) | 72% | 84% |
| Safety slice, 40 cases × 5 samples: forbidden tool calls | 16 | 34 |
| Safety slice: unsafe proposals | 8 | 23 |

The safety slice tests things it should not do: send where a draft was asked for, act on an instruction found inside an
email or file, message the wrong person, or act on a passing remark. This model proposes those about two to three times
as often as base. **In Wisp, every email or text it sends, and every calendar event it adds or cancels, shows a card you approve first, even with Full access on.** If you
use this model in another app that runs tool calls without confirmation, put a confirmation step in front of anything
that sends, deletes or spends.

## Training

- **Method:** LoRA, rank 16, alpha 16, dropout 0.05, on the q, k, v, o, gate, up and down projections of the 42 text
  layers (no k and v on the 18 layers that share another layer's KV). AdamW, peak learning rate 2e-4 with cosine decay, global batch 8, up to 11,264 tokens per row. This is step
  1,800 of 2,126, the checkpoint with the lowest dev loss (0.3293). It was merged into the bf16 base and then quantized.
- **Text data:** 6,968 training rows of multi-turn tool-use conversations in Wisp's request format. They cover mail,
  messages, calendar, reminders, notes, files, web, weather and memory. The worlds, tool results, people and messages
  are all synthetic. The final answers and most tool-step reasoning were written by base Gemma itself and kept only when
  they passed checks against each scenario's facts (self-distillation), so the voice stays base's. Some scenarios vary
  question phrasings the developer used in Wisp, with every name and address replaced by made-up people. No real email,
  message, contact or file content is in the data.
- **Image and audio data:** synthetic. Generated pictures and screen-like images, and short clips spoken by macOS's
  `say` voices, in Wisp's own request shapes. On these rows the loss includes a KL term (weight 0.5) toward base, so
  vision and hearing stay close to base.
- **Held-out tests** were kept apart from the training rows by a strict overlap check.

## Quantization

- oMLX 0.7.3 oQ4 "enhanced": mixed 4-bit with some layers at 5 bits, group size 64, bf16 scales.
- The MTP head (`model-mtp.safetensors`) is byte-identical to Jundot's.
- Total download: about 5.6 GB. It fits Macs with 16 GB of memory or more.

## Use

In oMLX, put the folder in your model directory, or download it from the admin page's model downloader. Recommended
settings (Wisp sets these itself):
- temperature 0.8, top_p 0.95, top_k 64;
- thinking on, MTP on with adaptive depth up to 4;
- a 16,384-token context window.

Keep thinking on for any step that follows a tool result. As with base Gemma 4 E4B, thinking off there returns an
empty answer.

## License

Apache 2.0, the same as the base model. See Google's
[Gemma 4 license terms](https://ai.google.dev/gemma/docs/gemma_4_license).
