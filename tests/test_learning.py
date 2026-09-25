import json
import numpy as np
import pytest
import torch
from torch.utils.tensorboard import SummaryWriter

from learning.core import Gems, Metrics, Repetition, Recording, SIZE, POINTS, WAIT, map_demo, static_mask
from learning.environment import GameEnv, SimulatedInterface
from learning.training import create, load_checkpoint, save_checkpoint, pretrain


def confirmed(gems, value, now):
    results = [gems.read(value, .99, now+i) for i in range(3)]
    assert results[:2] == [(0,False), (0,False)]
    return results[-1]


def test_gems_transition_restart_and_errors():
    g = Gems()
    assert confirmed(g,100,0) == (0,True)
    assert g.read(100,.99,3) == (0,True)
    assert g.read(None,0,4) == (0,False)
    assert g.read(999,.94,5) == (0,False)
    assert confirmed(g,105,6) == (5,True)
    assert g.read(105,.99,9) == (0,True)
    assert confirmed(g,110,75) == (0,True)
    g.reset()
    assert confirmed(g,110,80) == (0,True)
    assert confirmed(g,109,83) == (0,True)
    assert g.decreased


def test_streak_must_be_consecutive():
    g = Gems()
    for i,v in enumerate([2,2,None,2,2,3,3,None]):
        assert g.read(v,.99,i) == (0,False)
    assert g.last is None


def test_metrics_and_repetition():
    m = Metrics()
    assert m.add(60,5,True)['gems_per_min_5m'] == 5
    assert m.add(60,0,False)['gems_per_min_5m'] == 2.5
    assert m.unknown == 60
    r = Repetition()
    image = np.zeros((SIZE[1],SIZE[0],3), np.uint8)
    assert [r.penalty(0,image) for _ in range(3)] == [0,0,-.02]
    image[:] = 1
    assert r.penalty(0,image) == 0
    assert r.penalty(WAIT,image) == 0


def test_recording_budget_and_mapping(tmp_path):
    recording = Recording(tmp_path, budget_gb=1e-9)
    assert not recording.write(np.zeros((4,208,96),np.uint8),0,{})
    assert recording.full and not list(tmp_path.rglob('*.npz'))
    assert map_demo('hold',(170,300),(170,300),3.5) is None
    assert map_demo('drag',(0,0),(360,780),.3) is None
    assert static_mask()[WAIT]


def test_real_cnn_update_checkpoint_resume_and_evaluation(tmp_path):
    env = GameEnv(SimulatedInterface())
    model = create(env)
    model.checkpoint_directory = str(tmp_path/'checkpoints')
    before = {k:v.clone() for k,v in model.policy.state_dict().items()}
    model.learn(256)
    assert model.num_timesteps == 256
    assert any(not torch.equal(v,before[k]) for k,v in model.policy.state_dict().items())
    assert (tmp_path/'checkpoints'/'step-000000000256.zip').exists()
    path = save_checkpoint(model,tmp_path/'checkpoints')
    restored = load_checkpoint(path,GameEnv(SimulatedInterface()))
    assert restored.num_timesteps == 256
    assert restored.policy.optimizer.state_dict()['state']
    frozen = {k:v.clone() for k,v in restored.policy.state_dict().items()}
    obs,_ = env.reset()
    for _ in range(8):
        action,_ = restored.predict(obs,deterministic=True,action_masks=env.action_masks())
        obs,*_ = env.step(action)
    assert all(torch.equal(v,frozen[k]) for k,v in restored.policy.state_dict().items())
    restored.learn(256,reset_num_timesteps=False)
    assert restored.num_timesteps == 512


def test_pretrain_separate_sessions(tmp_path):
    env = GameEnv(SimulatedInterface())
    obs,_ = env.reset()
    action = int(np.flatnonzero(static_mask())[0])
    for i in range(2):
        rec = Recording(tmp_path/f'source-{i}')
        rec.write(obs, action, {})
        rec.session.rename(tmp_path/f'session-{i}')
        rec.root.rmdir()
    model = create(env)
    model.checkpoint_directory = str(tmp_path/'out')
    before = model.policy.action_net.weight.detach().clone()
    with SummaryWriter(str(tmp_path/'logs')) as writer:
        pretrain(model,tmp_path,1,writer)
    assert not torch.equal(before,model.policy.action_net.weight)


def test_unknown_screen_masks_everything_but_wait():
    class Unknown(SimulatedInterface):
        def snapshot(self):
            image,_,_,_ = super().snapshot()
            return image,None,0.,False
    env = GameEnv(Unknown())
    env.reset()
    assert np.flatnonzero(env.action_masks()).tolist() == [WAIT]
    _,reward,_,_,info = env.step(0)
    assert info['blocked'] == -.1
    assert info['gems'] == 0


def test_hotkeys_ignore_typing_outside_game(monkeypatch):
    from unittest.mock import Mock
    from pynput import keyboard
    from learning.environment import Controls
    listener = Mock()
    monkeypatch.setattr(keyboard, 'Listener', listener)
    controls = Controls(paused=False)
    controls.hotkeys_enabled = Mock(return_value=False)
    controls.start()
    press = listener.call_args.kwargs['on_press']
    for char in 'upgrades':
        press(Mock(char=char))
    assert not controls.stopped and not controls.paused
    controls.hotkeys_enabled.return_value = True
    press(Mock(char='z'))
    assert controls.paused
    press(Mock(char='p'))
    assert controls.stopped and controls.stop_reason == 'P in game window'
    controls.close()


def test_demonstration_weight_prioritizes_gameplay():
    import config
    from learning.training import demonstration_weight
    assert demonstration_weight({'kind':'click', 'start':list(config.IDLE_CLICK_POS)}) == .1
    assert demonstration_weight({'kind':'hold', 'start':[170,300]}) == 1.
    assert demonstration_weight({'kind':'click', 'start':[170,300]}) == 1.
