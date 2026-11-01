"""Convert an image to grayscale. The reference tool every other tool follows.

Arguments: `input`, `output` — paths to PNG files.
"""

import ops

a = ops.args()
image = ops.load(a["input"])
ops.grayscale(image)
ops.save(image, a["output"])
