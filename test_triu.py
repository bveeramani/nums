import nums.numpy as nps
import numpy as np
from nums.numpy import BlockArray
a: BlockArray = nps.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
a = a.reshape(block_shape=(4,))
print(nps.triu(a).get())