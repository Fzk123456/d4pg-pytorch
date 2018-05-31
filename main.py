import gym
import time
import argparse
from ddpg import DDPG
from utils import *
from shared_adam import SharedAdam
import numpy as np
from normalize_env import NormalizeAction
import torch.multiprocessing as mp

from pdb import set_trace as bp

# Parameters
parser = argparse.ArgumentParser(description='async_ddpg')

#parser.add_argument('--seed', type=int, default=1, help='random seed (default: 1)')
parser.add_argument('--n_workers', type=int, default=4, help='how many training processes to use (default: 4)')
parser.add_argument('--rmsize', default=50000, type=int, help='memory size')
#parser.add_argument('--init_w', default=0.003, type=float, help='')
#parser.add_argument('--window_length', default=1, type=int, help='')
parser.add_argument('--tau', default=0.001, type=float, help='moving average for target network')
parser.add_argument('--ou_theta', default=0.15, type=float, help='noise theta')
parser.add_argument('--ou_sigma', default=0.2, type=float, help='noise sigma')
parser.add_argument('--ou_mu', default=0.0, type=float, help='noise mu')
parser.add_argument('--bsize', default=64, type=int, help='minibatch size')
parser.add_argument('--discount', default=0.9, type=float, help='')
parser.add_argument('--env', default='Pendulum-v0', type=str, help='Environment to use')
parser.add_argument('--max_steps', default=500, type=int, help='Maximum steps per episode')
parser.add_argument('--n_eps', default=2000, type=int, help='Maximum number of episodes')
parser.add_argument('--debug', default=True, type=bool, help='Print debug statements')
#parser.add_argument('--epsilon', default=10000, type=int, help='linear decay of exploration policy')
parser.add_argument('--warmup', default=10000, type=int, help='time without training but only filling the replay memory')
parser.add_argument('--p_replay', default=True, type=bool, help='Enable prioritized replay - based on TD error')
#parser.add_argument('--prate', default=0.0001, type=float, help='policy net learning rate (only for DDPG)')
#parser.add_argument('--rate', default=0.001, type=float, help='learning rate')
#parser.add_argument('--load_weights', dest="load_weights", action='store_true', help='load weights for actor and critic')
#parser.add_argument('--shared', dest="shared", action='store_true')
#parser.add_argument('--use_more_states', dest="use_more_states", action='store_true')
#parser.add_argument('--num_states', default=4, type=int)


args = parser.parse_args()

env = NormalizeAction(gym.make(args.env).env)
env._max_episode_steps = args.max_steps
discrete = isinstance(env.action_space, gym.spaces.Discrete)

# Get observation and action space dimensions
obs_dim = env.observation_space.shape[0]
act_dim = env.action_space.n if discrete else env.action_space.shape[0]

critic_dist_info = {}
critic_dist_info['type']     = 'categorical'
critic_dist_info['v_min']    = -1000
critic_dist_info['v_max']    = 100
critic_dist_info['n_atoms'] = 51


global_returns = [(0, 0)]  # list of tuple(step, return)


def global_model_eval(global_model, global_count):
    temp_model = DDPG(obs_dim=obs_dim, act_dim=act_dim, critic_dist_info=critic_dist_info)

    while True:
        counter = to_numpy(global_count)[0]
        if counter >= 1000000:
            break

        temp_model.actor.load_state_dict(global_model.actor.state_dict())
        temp_model.critic.load_state_dict(global_model.critic.state_dict())

        temp_model.actor.eval()
        env = NormalizeAction(gym.make(args.env).env)
        env._max_episode_steps = 500
        # bp()
        global global_returns
        state = env.reset()
        curr_return = 0
        step_count = 0
        while True:
            action = temp_model.actor(to_tensor(state.reshape((1,-1))))
            next_state, reward, done, _ = env.step(to_numpy(action).reshape(-1))
            curr_return += reward
            step_count+=1
            # print("Step count: ", step_count)
            if done or step_count > args.max_steps:
                break
            else:
                state = next_state
        global_returns.append((counter, 0.95*global_returns[-1][1] + 0.05*curr_return))
        print("Global Steps: ", counter, "Global return: ", global_returns[-1][1])

        time.sleep(10)



class Worker(object):
    def __init__(self, name, optimizer_global_actor, optimizer_global_critic):
        self.env = NormalizeAction(gym.make(args.env).env)
        self.env._max_episode_steps = args.max_steps
        self.name = name
        self.ddpg = DDPG(obs_dim=obs_dim, act_dim=act_dim, env=self.env, memory_size=args.rmsize,\
                          batch_size=args.bsize, tau=args.tau, critic_dist_info=critic_dist_info, \
                          prioritized_replay=True)
        self.ddpg.assign_global_optimizer(optimizer_global_actor, optimizer_global_critic)
        print('Intialized worker :',self.name)

    # warmup function not used <- deprecated
    def warmup(self):
        n_steps = 0
        self.ddpg.actor.eval()
        # for i in range(args.n_eps):
        #     state = self.env.reset()
        #     for j in range(args.max_steps):
        #
        state = self.env.reset()
        for n_steps in range(args.warmup):
            action = np.random.uniform(-1.0, 1.0, size=act_dim)
            next_state, reward, done, _ = self.env.step(action)
            self.ddpg.replayBuffer.add(state, action, reward, next_state, done)

            if done:
                state = self.env.reset()
            else:
                state = next_state


    def work(self, global_ddpg, global_count):
        avg_reward = 0.
        n_steps = 0
        self.warmup()

        self.ddpg.sync_local_global(global_ddpg)
        self.ddpg.hard_update()
        for i in range(args.n_eps):
            state = self.env.reset()
            total_reward = 0.
            for j in range(args.max_steps):
                self.ddpg.actor.eval()

                state = state.reshape(1, -1)
                noise = self.ddpg.noise.sample()
                action = to_numpy(self.ddpg.actor(to_tensor(state))).reshape(-1, ) + noise
                next_state, reward, done, _ = self.env.step(action)
                total_reward += reward
                self.ddpg.replayBuffer.add(state.reshape(-1), action, reward, next_state, done)
                #self.ddpg.replayBuffer.append(state.reshape(-1), action, reward, done)

                self.ddpg.actor.train()
                self.ddpg.train(global_ddpg)
                global_count += 1

                n_steps += 1

                if done:
                    break

                state = next_state
                # print("Episode ", i, "\t Step count: ", n_steps)


            avg_reward = 0.95*avg_reward + 0.05*total_reward

            if i%1==0:
                print('Episode ',i,'\tWorker :',self.name,'\tAvg Reward :',avg_reward,'\tTotal reward :',total_reward,'\tSteps :',n_steps)


if __name__ == '__main__':
    global_ddpg = DDPG(obs_dim=obs_dim, act_dim=act_dim, env=env, memory_size=args.rmsize,\
                        batch_size=args.bsize, tau=args.tau, critic_dist_info=critic_dist_info)
    optimizer_global_actor = SharedAdam(global_ddpg.actor.parameters(), lr=(1e-4)/float(args.n_workers))
    optimizer_global_critic = SharedAdam(global_ddpg.critic.parameters(), lr=(1e-3)/float(args.n_workers))

    # optimizer_global_actor.share_memory()
    # optimizer_global_critic.share_memory()
    global_ddpg.share_memory()

    multiThread = True
    if not multiThread:
        worker = Worker(str(1), optimizer_global_actor, optimizer_global_critic)
        worker.work(global_ddpg)
    else:
        processes = []
        global_count = to_tensor(np.zeros(1), requires_grad=False).share_memory_()
        p = mp.Process(target=global_model_eval, args=[global_ddpg, global_count])
        p.start()
        processes.append(p)

        for i in range(args.n_workers):
            worker = Worker(str(i), optimizer_global_actor, optimizer_global_critic)
            p = mp.Process(target=worker.work, args=[global_ddpg, global_count])
            p.start()
            processes.append(p)
        for p in processes:
            p.join()
