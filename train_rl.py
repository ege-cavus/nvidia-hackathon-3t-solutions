"""Track 1: PPO starter. Run python train_rl.py --steps 2048."""
import argparse
from state_schema import get_initial_city_state


def main():
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_checker import check_env
    from sheffield_env import SheffieldEnv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=2048)
    parser.add_argument("--output", default="suru_ppo")
    args = parser.parse_args()
    if args.steps <= 0:
        parser.error("--steps must be positive")
    env = SheffieldEnv()
    check_env(env)
    model = PPO("MlpPolicy", env, seed=42, verbose=1, n_steps=128, batch_size=64)
    model.learn(total_timesteps=args.steps)
    model.save(args.output)
    print("Initial mode:", get_initial_city_state()["mode"])


if __name__ == "__main__":
    main()
