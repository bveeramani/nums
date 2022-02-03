import numpy as np

from nums.core.array.application import ArrayApplication
from nums.core.array.blockarray import BlockArray, Block
from nums.core.systems.systems import RaySystem


# pylint: disable=protected-access


def test_warmup(app_inst_all: ArrayApplication):
    sys = app_inst_all.cm.system
    if isinstance(sys, RaySystem):
        sys.warmup(10)
    assert True


def test_transposed_block(app_inst_all: ArrayApplication):
    ba: BlockArray = app_inst_all.array(
        np.array([[1, 2, 3], [4, 5, 6]]), block_shape=(1, 3)
    )
    block1: Block = ba.T.blocks[0, 1]
    assert block1.size() == 3
    assert not block1.transposed
    assert block1.grid_entry == block1.true_grid_entry()
    assert block1.grid_shape == block1.true_grid_shape()


def test_deferred_transposed_block(app_inst_all: ArrayApplication):
    ba: BlockArray = app_inst_all.array(
        np.array([[1, 2, 3], [4, 5, 6]]), block_shape=(1, 3)
    )
    block1: Block = ba.transpose(defer=True).blocks[0, 1]
    assert block1.size() == 3
    assert block1.transposed
    assert block1.grid_entry == (0, 1)
    assert block1.grid_shape == (1, 2)
    assert block1.true_grid_entry() == (1, 0)
    assert block1.true_grid_shape() == (2, 1)


if __name__ == "__main__":
    # pylint: disable=import-error
    import conftest

    app_inst = conftest.get_app("ray-none")
    test_warmup(app_inst)
    test_deferred_transposed_block(app_inst)
