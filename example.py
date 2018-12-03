# Open Set Learning with Counterfactual Images
# First, train a classifier on the K known classes
# Then train the counterfactual generative model
# Then generate counterfactual open set images
# Then reparameterize and re-train the classifier for open set classification
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import torchvision

import imutil
from logutil import TimeSeries
from datasetutil.dataloader import CustomDataloader


BATCH_SIZE = 64
EMNIST_LOCATION = '/mnt/nfs/data/emnist.dataset'


class Classifier(nn.Module):
    def __init__(self, num_classes):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, num_classes)
        self.cuda()

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return x


class Encoder(nn.Module):
    def __init__(self, latent_size):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, latent_size)
        self.cuda()

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        x = norm(x)
        return x


# Project to the unit sphere
def norm(x):
    norm = torch.norm(x, p=2, dim=1)
    x = x / (norm.expand(1, -1).t() + .0001)
    return x


class Generator(nn.Module):
    def __init__(self, latent_size):
        super().__init__()
        self.fc1 = nn.Linear(latent_size, 128)
        self.fc2 = nn.Linear(128, 196)
        self.conv1 = nn.ConvTranspose2d(4, 32, stride=2, kernel_size=4, padding=1)
        self.conv2 = nn.ConvTranspose2d(32, 1, stride=2, kernel_size=4, padding=1)
        self.cuda()

    def forward(self, x):
        x = self.fc1(x)
        x = F.leaky_relu(x, 0.2)
        x = self.fc2(x)
        x = F.leaky_relu(x, 0.2)
        x = x.view(-1, 4, 7, 7)
        x = self.conv1(x)
        x = F.leaky_relu(x, 0.2)
        x = self.conv2(x)
        x = torch.sigmoid(x)
        return x


class Discriminator(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 1)
        self.cuda()

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


def main():
    # Train and test a perfectly normal, ordinary classifier
    classifier = Classifier(num_classes=10)
    train_classifier(classifier, load_training_dataset())
    test_open_set_performance(classifier)

    # Build a generative model
    encoder = Encoder(latent_size=10)
    generator = Generator(latent_size=10)
    discriminator = Discriminator()
    train_generative_model(encoder, generator, discriminator, load_training_dataset())

    # Generate counterfactual open set images
    open_set_images = generate_counterfactuals(encoder, generator, classifier, load_training_dataset())

    # Use counterfactual open set images to re-train the classifier
    open_set_classifier = Classifier(num_classes=11)
    train_open_set_classifier(open_set_classifier, load_training_dataset(), open_set_images)

    test_open_set_performance(open_set_classifier, mode='augmented_classifier')


def load_training_dataset():
    transform = torchvision.transforms.Compose([
        torchvision.transforms.ToTensor(),
    ])
    dataset = torchvision.datasets.MNIST('../data', train=True, download=True, transform=transform)
    dataloader = torch.utils.data.DataLoader(dataset=dataset, batch_size=BATCH_SIZE, shuffle=True)
    def generator():
        for images, labels in dataloader:
            yield images.cuda(), labels.cuda()
    return generator()


def load_testing_dataset():
    transform = torchvision.transforms.Compose([
        torchvision.transforms.ToTensor(),
    ])
    dataset = torchvision.datasets.MNIST('../data', train=False, download=True, transform=transform)
    dataloader = torch.utils.data.DataLoader(dataset=dataset, batch_size=BATCH_SIZE, shuffle=False)
    def generator():
        for images, labels in dataloader:
            yield images.cuda(), labels.cuda()
    return generator()


def load_open_set():
    def generator():
        for images, labels in CustomDataloader(EMNIST_LOCATION, fold='test', image_size=28, shuffle=False):
            labels[:] = 10
            yield torch.Tensor(images).cuda().mean(dim=1).unsqueeze(1), torch.LongTensor(labels).cuda()
    return generator()


def train_classifier(classifier, dataset):
    adam = torch.optim.Adam(classifier.parameters())
    for images, labels in dataset:
        adam.zero_grad()
        preds = F.log_softmax(classifier(images), dim=1)
        classifier_loss = F.nll_loss(preds, labels)
        classifier_loss.backward()
        adam.step()
        print('classifier loss: {}'.format(classifier_loss))


def test_classifier(classifier, dataset):
    total = 0
    total_correct = 0
    for images, labels in dataset:
        preds = classifier(images)
        correct = torch.sum(preds.max(dim=1)[1] == labels)
        total += len(images)
        total_correct += correct
    accuracy = float(total_correct) / total
    print('Test Accuracy: {}/{} ({:.03f})'.format(total_correct, total, accuracy))


def test_open_set_performance(classifier, mode='confidence_threshold'):
    known_scores = []
    for images, labels in load_testing_dataset():
        preds = classifier(images)
        known_scores.extend(get_score(preds, mode))

    unknown_scores = []
    for images, labels in load_open_set():
        preds = classifier(images)
        unknown_scores.extend(get_score(preds, mode))

    auc = plot_roc(known_scores, unknown_scores, mode)
    print('Detecting with mode {}, avg. known-class score: {}, avg unknown score: {}'.format(
        mode, np.mean(known_scores), np.mean(unknown_scores)))
    print('Mode {}: generated ROC with AUC score {:.03f}'.format(mode, auc))


def plot_roc(known_scores, unknown_scores, mode):
    from sklearn.metrics import roc_curve, roc_auc_score
    y_true = np.array([0] * len(known_scores) + [1] * len(unknown_scores))
    y_score = np.concatenate([known_scores, unknown_scores])
    fpr, tpr, thresholds = roc_curve(y_true, y_score)
    auc_score = roc_auc_score(y_true, y_score)
    title = 'ROC {}: AUC {:.03f}'.format(mode, auc_score)
    plot = plot_xy(fpr, tpr, x_axis="False Positive Rate", y_axis="True Positive Rate", title=title)
    filename = 'roc_{}.png'.format(mode)
    plot.figure.savefig(filename)
    return auc_score


def plot_xy(x, y, x_axis="X", y_axis="Y", title="Plot"):
    import pandas as pd
    # Hack to keep matplotlib from crashing when run without X
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    # Apply sane defaults to matplotlib
    import seaborn as sns
    sns.set_style('darkgrid')

    # Generate plot
    df = pd.DataFrame({'x': x, 'y': y})
    plot = df.plot(x='x', y='y')
    plot.grid(b=True, which='major')
    plot.grid(b=True, which='minor')
    plot.set_title(title)
    plot.set_ylabel(y_axis)
    plot.set_xlabel(x_axis)
    return plot


def get_score(preds, mode):
    if mode == 'confidence_threshold':
        return torch.max(torch.softmax(preds, dim=1), dim=1)[0].data.cpu().numpy()
    elif mode == 'augmented_classifier':
        return torch.softmax(preds, dim=1)[:, -1].data.cpu().numpy()
    assert False


def train_open_set_classifier(classifier, dataset, open_set_images):
    adam = torch.optim.Adam(classifier.parameters())
    for (images, labels), open_set_images in zip(dataset, open_set_images):
        adam.zero_grad()
        preds = F.log_softmax(classifier(images), dim=1)
        classifier_loss = F.nll_loss(preds, labels)

        batch_size, num_classes = preds.shape
        open_set_labels = torch.LongTensor(batch_size).cuda()
        open_set_labels[:] = num_classes - 1
        open_set_loss = F.nll_loss(preds, open_set_labels)

        loss = classifier_loss + open_set_loss
        loss.backward()
        adam.step()
        print('open set classifier loss: {}'.format(loss))
    print('Finished training open-set-augmented classifier')


def train_generative_model(encoder, generator, discriminator, dataset):
    params = [x for x in encoder.parameters()] + [x for x in generator.parameters()]
    adam = torch.optim.Adam(params)
    for images, labels in dataset:
        adam.zero_grad()
        mse = (generator(encoder(images)) - images) ** 2
        loss = torch.mean(mse)
        loss.backward()
        print('Autoencoder loss: {:.03f}'.format(loss))
        adam.step()
    print('Autoencoder training finished')


def generate_counterfactuals(encoder, generator, classifier, dataset):
    cf_open_set_images = []
    for images, labels in dataset:
        counterfactuals = generate_cf( encoder, generator, classifier, images)
        cf_open_set_images.append(counterfactuals)
    print("Generated {} batches of counterfactual images".format(len(cf_open_set_images)))
    return cf_open_set_images


def generate_cf(encoder, generator, classifier, images,
                cf_iters=100, cf_step_size=.01, cf_distance_weight=1.0):
    from torch.autograd import grad

    # First encode the image into latent space (z)
    z_0 = encoder(images)
    z = z_0.clone()

    # Now perform gradient descent to update z
    for i in range(cf_iters):
        # Classify with one extra class
        logits = classifier(generator(z))
        augmented_logits = F.pad(logits, pad=(0,1))

        # Use the extra class as a counterfactual target
        batch_size, num_classes = logits.shape
        target_tensor = torch.LongTensor(batch_size).cuda()
        target_tensor[:] = num_classes

        # Maximize classification probability of the counterfactual target
        cf_loss = F.nll_loss(F.log_softmax(augmented_logits, dim=1), target_tensor)

        # Regularize with distance to original z
        distance_loss = torch.mean((z - z_0) ** 2)

        # Move z toward the "open set" class
        loss = cf_loss + distance_loss
        dc_dz = grad(loss, z, loss)[0]
        z -= cf_step_size * dc_dz

        # Sanity check: Clip gradients to avoid nan in ill-conditioned inputs
        #dc_dz = torch.clamp(dc_dz, -.1, .1)

        # Optional: Normalize to the unit sphere (match encoder's settings)
        z = norm(z)

    print("Generated batch of counterfactual images with cf_loss {:.03f}".format(cf_loss))
    # Output the generated image as an example "unknown" image
    return generator(z).detach()


if __name__ == '__main__':
    main()
