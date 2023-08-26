import torch

from accelerate.utils import set_seed, reduce

from template.util import load_module, show_device, show_batch_size
from template.util.custom_accelerator import SimplerAccelerator
from template.util.rich import MainConsole, track
from template.util.tracker import SysTracker


__all__ = ['train', 'val', 'predict', 'info', 'hyper_search']

console = MainConsole(color_system='auto', log_time_format='[%Y.%m.%d %H:%M:%S]')


def train(config: str, epoch=24, seed=3407, batch_per_gpu=64, num_workers=4, grad_step=1, wandb=False, tb=False,
          resume=''):
    with console.status("Loading modules...", spinner="aesthetic", spinner_style='cyan'):
        module_loader = load_module(config)

    # load configuration from the module
    saver_config = module_loader.saver_config

    # generate save_dir with current time
    saver_config.generate_config_path(config=config)

    # gradient clip
    grad_clipper =module_loader.grad_clipper

    # add loggers
    loggers = [SysTracker(logdir=saver_config.project_dir)]
    if wandb:
        loggers.append('wandb')
    if tb:
        loggers.append('tensorboard')

    accelerator = SimplerAccelerator(config=config,
                                     log_with=loggers,
                                     saver_config=saver_config,
                                     gradient_accumulation_steps=grad_step,
                                     grad_clipper=grad_clipper)
    # seed all
    set_seed(seed, device_specific=True)

    model = module_loader.model
    loss_fn = module_loader.loss_fn
    train_set = module_loader.train_set
    test_set = module_loader.test_set
    optimizer = module_loader.optimizer
    iter_scheduler = module_loader.iter_scheduler
    epoch_scheduler = module_loader.epoch_scheduler
    metric = module_loader.metric

    # basic info for the wandb log
    tracker_config = dict(
        config=config,
        epoch=epoch,
        resume=resume,
        seed=seed,
        num_workers=num_workers,
        wandb=wandb,
        tb=tb,
        model=getattr(model, 'default_cfg', model.__class__.__name__),
        loss=loss_fn.name,
        optimizer=dict(name=optimizer.__class__.__name__,
                       **optimizer.defaults),
        iter_scheduler=dict(name=iter_scheduler.__class__.__name__,
                            **iter_scheduler.__dict__),
        epoch_scheduler=dict(name=epoch_scheduler.__class__.__name__,
                             **epoch_scheduler.__dict__),
        learning_rate=module_loader.lr,
        dataset=repr(train_set),
        batch=dict(batch_per_gpu=batch_per_gpu,
                          num_proc=accelerator.num_processes,
                          gradient_accumulation_step=grad_step,
                          total_batch_size=batch_per_gpu * accelerator.num_processes * grad_step),
        grad_clip=grad_clipper,
        saver=saver_config.to_dict(),
        distributed_type=accelerator.distributed_type.value,
        precision=accelerator.mixed_precision,
        num_proc=accelerator.num_processes,
        dynamo=accelerator.state.dynamo_plugin.to_dict()
    )
    console.print(tracker_config)

    accelerator.init_trackers(project_name=module_loader.wandb_log_name,
                              config=tracker_config)

    if wandb and accelerator.is_local_main_process:
        wandb_tracker = accelerator.get_tracker("wandb", unwrap=True)
        wandb_tracker.save(config)

    console.print(show_device(), justify="center")
    console.print(show_batch_size(tracker_config['batch']), justify="center")

    accelerator.free_memory()
    torch.backends.cudnn.benchmark = True
    train_loader = torch.utils.data.DataLoader(dataset=train_set, batch_size=batch_per_gpu,
                                               num_workers=num_workers,
                                               collate_fn=train_set.collate_fn, shuffle=True, drop_last=True,
                                               pin_memory=True)
    test_loader = torch.utils.data.DataLoader(dataset=test_set, batch_size=batch_per_gpu,
                                              num_workers=num_workers,
                                              collate_fn=test_set.collate_fn, shuffle=False, drop_last=False,
                                              pin_memory=True)

    with console.status("Prepare everything...", spinner="aesthetic", spinner_style='cyan'):
        model = accelerator.prepare(model)
        optimizer, iter_scheduler, epoch_scheduler, train_loader, test_loader \
            = accelerator.prepare(optimizer, iter_scheduler, epoch_scheduler, train_loader, test_loader)
        epoch_scheduler.step_with_optimizer = False

    # resume from checkpoint
    start_epoch = 0
    if resume:
        accelerator.load_state(resume)
        start_epoch = int(resume.split('_')[-1]) + 1
        console.log(f"Resume from: {resume}, start epoch: {start_epoch}")

    loss_dict = {}

    del module_loader

    # start training
    console.rule("[bold red]Start Training![/bold red]:fire:", style='#FF3333')
    for e in range(start_epoch, epoch):
        model.train()
        loss_fn.train()
        for x, y in track(train_loader, description=f'Train epoch {e}',
                          disable=not accelerator.is_local_main_process):
            with accelerator.accumulate(model):
                outputs = model(x)
                loss_dict = loss_fn(outputs, y)
                accelerator.backward(loss_dict['loss'])
                optimizer.step()
                optimizer.zero_grad()
                iter_scheduler.step()
        epoch_scheduler.step()

        train_loss_dict = reduce(loss_dict)  # reduce the loss among all devices
        train_loss_dict = {k: v.item() for k, v in train_loss_dict.items()}

        model.eval()
        loss_fn.eval()
        with torch.no_grad():
            for x, label in track(test_loader, description=f'Test epoch {e}',
                                  disable=not accelerator.is_local_main_process):
                outputs = model(x)
                all_outputs, all_label = accelerator.gather_for_metrics((outputs, label))
                metric.add_batch(pred=all_outputs, label=all_label)

        metrics = metric.compute()

        trace_log = dict(
            **metrics,
            **train_loss_dict,
            learning_rate=optimizer.optimizer.param_groups[0]['lr'],
        )

        accelerator.log(trace_log, step=e)

        # save latest checkpoint
        accelerator.save_state()
        # save best model
        accelerator.save_best_model(metrics, model)

        accelerator.wait_for_everyone()

    console.rule("[bold dodger_blue3]Finish Training![/bold dodger_blue3]:ok:", style="cyan")
    accelerator.end_training()


@torch.no_grad()
def val(config: str, load_from: str, seed=3407, batch_per_gpu=64, num_workers=4):
    with console.status("Loading modules...", spinner="aesthetic", spinner_style='cyan'):
        module_loader = load_module(config)
    # load configuration from the module
    saver_config = module_loader.saver_config
    # generate save_dir with current time
    saver_config.generate_config_path(config=config)

    accelerator = SimplerAccelerator(config=config,
                                     log_with=SysTracker(logdir=saver_config.project_dir),
                                     saver_config=saver_config)
    set_seed(seed, device_specific=True)
    accelerator.init_trackers(project_name='testing')

    accelerator.free_memory()
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    model = module_loader.model
    test_set = module_loader.test_set
    metric = module_loader.metric

    del module_loader

    model.load_state_dict(torch.load(load_from, map_location="cpu"))

    test_loader = torch.utils.data.DataLoader(dataset=test_set, batch_size=batch_per_gpu,
                                              num_workers=num_workers,
                                              collate_fn=test_set.collate_fn, shuffle=False, drop_last=False,
                                              pin_memory=True)
    with console.status("Prepare everything...", spinner="aesthetic", spinner_style='cyan'):
        model, test_loader = accelerator.prepare(model, test_loader)

    model.eval()

    for x, label in track(test_loader, description=f'Testing', disable=not accelerator.is_local_main_process):
        outputs = model(x)
        all_outputs, all_label = accelerator.gather_for_metrics((outputs, label))
        metric.add_batch(pred=all_outputs, label=all_label)
    metrics = metric.compute()


    accelerator.log(metrics, step=0)
    accelerator.end_training()


def predict(config: str, img: str, load_from: str):
    import cv2
    import albumentations as A
    from albumentations.pytorch import ToTensorV2
    from template.visualizer import ImageClassificationVisualizer

    raw_img = cv2.imread(img, cv2.IMREAD_COLOR)

    module_loader = load_module(config)

    model = module_loader.model
    model.load_state_dict(torch.load(load_from, map_location="cpu"))
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device)

    vis = ImageClassificationVisualizer()

    # preprocess
    image = cv2.cvtColor(raw_img, cv2.COLOR_BGR2RGB)
    transform = A.Compose([
        A.Resize(module_loader.img_size[-2], module_loader.img_size[-1]),
        A.Normalize(mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225)),
        ToTensorV2()])
    image = transform(image=image)['image']
    image = torch.unsqueeze(image, 0)
    image = image.to(device)
    with torch.no_grad():
        output = model(image)

    # post process
    out_idx = torch.argmax(output, -1)
    out_idx = torch.squeeze(out_idx, 0).cpu().item()
    out_class = module_loader.classes[out_idx]

    vis.draw(image=raw_img, label=out_class)


def info(config: str, batch_per_gpu=1):
    import torch
    from torchinfo import summary
    from ptflops import get_model_complexity_info

    with console.status("Loading modules...", spinner="aesthetic", spinner_style='cyan'):
        module_loader = load_module(config)
    model = module_loader.model
    img_size: tuple = module_loader.img_size
    input_size = (batch_per_gpu,) + img_size
    del module_loader

    with torch.cuda.device(0):
        with console.status("Calculating...", spinner="aesthetic", spinner_style='cyan'):
            summary(model, input_size)
            macs, params = get_model_complexity_info(model, img_size, as_strings=True,
                                                     print_per_layer_stat=True, verbose=True)
        print('{:<30}  {:<8}'.format('Computational complexity: ', macs))
        print('{:<30}  {:<8}'.format('Number of parameters: ', params))


def hyper_search(config: str, epoch=24, seed=3407, n_trials=3, wandb=False):
    import optuna
    from optuna.integration.wandb import WeightsAndBiasesCallback
    from modelings.losses import ClsLoss

    with console.status("Loading modules...", spinner="aesthetic", spinner_style='cyan'):
        module_loader = load_module(config)

    wandbc = WeightsAndBiasesCallback(wandb_kwargs={"project": module_loader.wandb_log_name},
                                      as_multirun=True) if wandb else None
    accelerator = SimplerAccelerator(config=config)

    def objective(trial: optuna.trial.Trial):
        batch_per_gpu = trial.suggest_int('batch_per_gpu', low=32, high=256)
        lr = trial.suggest_float('lr', low=1e-5, high=1e-2, step=1e-5)
        optimizer_category = trial.suggest_categorical('optimizer', choices=['Adam', 'AdamW', 'SGD'])
        loss_fn_category = trial.suggest_categorical('loss_fn', choices=['ClsMSE', 'ClsCrossEntropy'])

        model = module_loader.model
        model.init_weights()
        loss_fn = getattr(ClsLoss, loss_fn_category)()
        train_set = module_loader.train_set
        test_set = module_loader.test_set
        optimizer = getattr(torch.optim, optimizer_category)(params=model.parameters(), lr=lr)
        metric = module_loader.metric

        set_seed(seed, device_specific=True)

        accelerator.free_memory()

        torch.backends.cudnn.benchmark = True
        train_loader = torch.utils.data.DataLoader(dataset=train_set, batch_size=batch_per_gpu,
                                                   num_workers=4,
                                                   collate_fn=train_set.collate_fn, shuffle=True, drop_last=True,
                                                   pin_memory=True)
        test_loader = torch.utils.data.DataLoader(dataset=test_set, batch_size=batch_per_gpu,
                                                  num_workers=4,
                                                  collate_fn=test_set.collate_fn, shuffle=False, drop_last=False,
                                                  pin_memory=True)

        with console.status("Prepare everything...", spinner="aesthetic", spinner_style='cyan'):
            model, optimizer, train_loader, test_loader \
                = accelerator.prepare(model, optimizer, train_loader, test_loader)

        model.train()
        loss_fn.train()
        for e in range(epoch):
            for x, y in train_loader:
                outputs = model(x)
                loss_dict = loss_fn(outputs, y)
                accelerator.backward(loss_dict['loss'])
                optimizer.step()
                optimizer.zero_grad()

        model.eval()
        loss_fn.eval()
        with torch.no_grad():
            for x, label in test_loader:
                outputs = model(x)
                all_outputs, all_label = accelerator.gather_for_metrics((outputs, label))
                metric.add_batch(pred=all_outputs, label=all_label)

        metrics = metric.compute()
        return metrics[module_loader.saver_config.monitor]

    study = optuna.create_study(direction='maximize' if module_loader.saver_config.higher_is_better else 'minimize')
    study.optimize(objective, n_trials=n_trials, callbacks=[wandbc])

    trial = study.best_trial
    print(f"Value:{trial.value}")
    print(" Params:")
    for key, value in trial.params.items():
        print("{}: {}".format(key, value))


def show_gpu():
    console.print(show_device(), justify='center')
