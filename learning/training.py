import json
from pathlib import Path

import numpy as np
import torch
import config
from sb3_contrib import MaskablePPO
from learning.core import ACTION_VERSION, fingerprint, static_mask


def demonstration_weight(metadata):
    """Keep useful close-panel examples without letting repeated idle taps dominate."""
    return .1 if (metadata.get('kind') == 'click' and
                  tuple(metadata.get('start', ())) == config.IDLE_CLICK_POS) else 1.


class Learner(MaskablePPO):
    """Save after optimization, not in the pre-update rollout callback."""
    checkpoint_directory = None

    def train(self):
        super().train()
        self.logger.dump(self.num_timesteps)
        if self.checkpoint_directory:
            save_checkpoint(self, self.checkpoint_directory)


def create(env, logs=None):
    torch.set_num_threads(4)
    model = Learner('CnnPolicy', env, device='cpu', learning_rate=.0001,
                    n_steps=256, batch_size=64, n_epochs=4, gamma=.995,
                    tensorboard_log=str(logs) if logs else None, verbose=1,
                    policy_kwargs={'features_extractor_kwargs': {'features_dim': 128}})
    model.pipeline_version = ACTION_VERSION
    model.configuration_hash = fingerprint()
    return model


def save_checkpoint(model, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'step-{model.num_timesteps:012d}.zip'
    temporary = directory / 'pending.zip'
    model.save(temporary)
    temporary.replace(target)
    pointer = directory / 'latest.tmp'
    pointer.write_text(target.name)
    pointer.replace(directory / 'latest.txt')
    return target


def load_checkpoint(path, env):
    path = Path(path)
    if path.is_dir():
        path = path / (path / 'latest.txt').read_text().strip()
    model = Learner.load(path, env=env, device='cpu')
    if (getattr(model, 'pipeline_version', None) != ACTION_VERSION or
            getattr(model, 'configuration_hash', None) != fingerprint()):
        raise ValueError('Checkpoint action/configuration version mismatch')
    torch.set_num_threads(4)
    return model


def pretrain(model, root, epochs, writer):
    sessions = [sorted(p.glob('*.npz')) for p in sorted(Path(root).iterdir()) if p.is_dir()]
    usable_sessions = []
    for session in sessions:
        usable = []
        for path in session:
            with np.load(path, allow_pickle=False) as data:
                action = int(data['action'])
                if 0 <= action < len(static_mask()) and static_mask()[action]:
                    usable.append(path)
        if usable:
            usable_sessions.append(usable)
    sessions = usable_sessions
    if len(sessions) < 2:
        raise ValueError('Collect at least two separate sessions with usable actions for train/validation split')
    split = max(1, int(len(sessions)*.8))
    training = [p for session in sessions[:split] for p in session]
    validation = [p for session in sessions[split:] for p in session]
    mask = torch.as_tensor(static_mask()[None, :], device=model.device)
    rng = np.random.default_rng(42)

    def batches(paths):
        for start in range(0, len(paths), 64):
            observations, actions, weights = [], [], []
            for path in paths[start:start+64]:
                with np.load(path, allow_pickle=False) as data:
                    meta = json.loads(str(data['metadata']))
                    if meta['config'] != fingerprint() or meta['version'] != ACTION_VERSION:
                        raise ValueError(f'Incompatible demonstration: {path}')
                    action = int(data['action'])
                    if not 0 <= action < len(static_mask()) or not static_mask()[action]:
                        continue
                    observations.append(data['observation'])
                    actions.append(action)
                    weights.append(demonstration_weight(meta))
            if actions:
                yield (torch.as_tensor(np.stack(observations), device=model.device),
                       torch.tensor(actions, device=model.device),
                       torch.tensor(weights, device=model.device))

    for epoch in range(epochs):
        rng.shuffle(training)
        for label, paths in (('train', training), ('validation', validation)):
            model.policy.set_training_mode(label == 'train')
            losses = []
            with torch.set_grad_enabled(label == 'train'):
                for obs, actions, weights in batches(paths):
                    distribution = model.policy.get_distribution(obs, action_masks=mask.expand(len(actions), -1))
                    loss = -(distribution.log_prob(actions)*weights).sum()/weights.sum()
                    if label == 'train':
                        model.policy.optimizer.zero_grad()
                        loss.backward()
                        torch.nn.utils.clip_grad_norm_(model.policy.parameters(), .5)
                        model.policy.optimizer.step()
                    losses.append(loss.item())
            if not losses:
                raise ValueError(f'No compatible {label} examples')
            writer.add_scalar('imitation/'+label+'_loss', np.mean(losses), epoch)
            print(f'Imitation epoch {epoch+1}/{epochs} {label} loss: {np.mean(losses):.5f}')
        save_checkpoint(model, model.checkpoint_directory)
