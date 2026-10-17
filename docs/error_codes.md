# Engine error codes

| Code | Meaning |
|---|---|
| `BAD_RANGE` | A time range or reorder destination has invalid bounds or shape. |
| `CUT_ALL` | The operation would remove every frame from the video. |
| `EDIT_CONFLICT` | The target conflicts with content removed by an earlier edit. |
| `INVALID_ARGUMENT` | An operation has a missing, malformed, or incorrectly typed argument. |
| `INVALID_SPEED` | The speed factor is not a finite number in the supported range. |
| `NO_CHANGE` | The operation is redundant, was already applied, or was declined without changing the timeline. |
| `NO_MATCH` | No matching content, edit, caption, or pending question was found. |
| `NO_TARGET` | The requested target does not exist, such as styling when there is no caption. |
| `NOTHING_TO_REDO` | There is no edit available to redo. |
| `NOTHING_TO_UNDO` | There is no edit available to undo. |
| `OUT_OF_RANGE` | A requested start or destination is beyond the video. |
| `UNSUPPORTED_OPERATION` | The instruction names an operation the engine does not support. |
